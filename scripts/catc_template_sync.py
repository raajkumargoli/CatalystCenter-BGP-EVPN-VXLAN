#!/usr/bin/env python3
"""Sync this repository's Catalyst Center Jinja templates into Template Editor."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_PROJECT_NAME = "BGP EVPN GitHub"
DEFAULT_TEMPLATE_DIR = "BGP EVPN"
DEFAULT_BUILD_FILE = "BGP EVPN/BGP-EVPN-BUILD.yml"
DEFAULT_DEVICE_TYPES = [
    {"productFamily": "Switches and Hubs", "productSeries": "Cisco Catalyst 9300 Series Switches"},
    {"productFamily": "Switches and Hubs", "productSeries": "Cisco Catalyst 9400 Series Switches"},
    {"productFamily": "Switches and Hubs", "productSeries": "Cisco Catalyst 9500 Series Switches"},
    {"productFamily": "Switches and Hubs", "productSeries": "Cisco Catalyst 9600 Series Switches"},
    {"productFamily": "Switches and Hubs", "productSeries": "Cisco Catalyst 9000 Series Virtual Switches"},
]
CATC_METADATA_RE = re.compile(r"\{##\s*CATC:\s*(?P<body>.*?)\s*##\}")
INCLUDE_RE = re.compile(
    r'{%\s*include\s+"(?:{{\s*TEMPLATE_PROJECT_NAME\s*}}|[^"]+?)/(?P<name>[^"/]+\.j2)"\s*%}'
)


class CatcError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CatalystCenter:
    def __init__(self, host: str, username: str, password: str, verify_tls: bool, timeout: int):
        self.base_url = f"https://{host.strip().removeprefix('https://').removeprefix('http://').rstrip('/')}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.token: str | None = None
        self.ssl_context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()

    def authenticate(self) -> None:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        auth = base64.b64encode(raw).decode("ascii")
        data = self._request(
            "POST",
            "/dna/system/api/v1/auth/token",
            headers={"Authorization": f"Basic {auth}"},
            token=False,
        )
        token = data.get("Token") or data.get("token") or data.get("response", {}).get("token")
        if not token:
            raise CatcError(f"Authentication succeeded but no token was returned: {data!r}")
        self.token = token

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, payload=payload)

    def put(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PUT", path, payload=payload)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        token: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        request_headers = {"Accept": "application/json"}
        if token:
            if not self.token:
                raise CatcError("Catalyst Center token is not set")
            request_headers["X-Auth-Token"] = self.token
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise CatcError(f"{method} {path} failed with HTTP {exc.code}: {raw}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise CatcError(f"{method} {path} failed: {exc.reason}") from exc


def task_id_from(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    nested = response.get("response")
    if isinstance(nested, dict):
        value = nested.get("taskId")
        if isinstance(value, str):
            return value
    value = response.get("taskId")
    return value if isinstance(value, str) else None


def wait_task(client: CatalystCenter, task_id: str | None, label: str, timeout: int = 120) -> None:
    if not task_id:
        return
    deadline = time.time() + timeout
    last_progress = ""
    while time.time() < deadline:
        data = client.get(f"/dna/intent/api/v1/task/{task_id}")
        response = data.get("response", data) if isinstance(data, dict) else {}
        if response.get("isError"):
            progress = response.get("progress") or response.get("failureReason") or response
            raise CatcError(f"Task failed for {label}: {progress}")
        last_progress = str(response.get("progress") or response.get("additionalStatusURL") or last_progress)
        if response.get("endTime"):
            return
        time.sleep(2)
    raise CatcError(f"Timed out waiting for {label} task {task_id}; last progress: {last_progress}")


def project_list(client: CatalystCenter) -> list[dict[str, Any]]:
    data = client.get("/dna/intent/api/v1/template-programmer/project")
    if isinstance(data, list):
        return data
    response = data.get("response", []) if isinstance(data, dict) else []
    return response if isinstance(response, list) else []


def find_project(client: CatalystCenter, project_name: str) -> dict[str, Any] | None:
    for project in project_list(client):
        if project.get("name") == project_name:
            return project
    return None


def ensure_project(client: CatalystCenter, project_name: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        print(f"Dry run: would ensure project {project_name}")
        return {"id": "dry-run-project-id", "name": project_name, "templates": []}
    project = find_project(client, project_name)
    if project:
        print(f"Using existing project: {project_name} ({project['id']})")
        return project
    print(f"Creating project: {project_name}")
    payload = {"name": project_name, "description": "BGP EVPN VXLAN templates synced from GitHub"}
    try:
        result = client.post("/dna/intent/api/v1/projects", payload)
    except CatcError as exc:
        if exc.status not in {404, 405}:
            raise
        result = client.post("/dna/intent/api/v1/template-programmer/project", payload)
    wait_task(client, task_id_from(result), f"create project {project_name}")
    for _ in range(15):
        project = find_project(client, project_name)
        if project:
            return project
        time.sleep(2)
    raise CatcError(f"Project {project_name!r} was created but not returned by the project API")


def parse_catc_metadata(content: str) -> dict[str, str]:
    match = CATC_METADATA_RE.search(content[:300])
    if not match:
        return {}
    fields: dict[str, str] = {}
    for item in match.group("body").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def render_template_content(path: Path, template_dir: Path, project_name: str, seen: tuple[Path, ...] = ()) -> str:
    content = path.read_text(encoding="utf-8")

    def replace_include(match: re.Match[str]) -> str:
        include_name = match.group("name")
        include_path = (template_dir / include_name).resolve()
        if not include_path.exists():
            return match.group(0).replace("{{ TEMPLATE_PROJECT_NAME }}", project_name)
        if include_path in seen:
            chain = " -> ".join(item.name for item in (*seen, include_path))
            raise CatcError(f"Recursive template include detected: {chain}")
        included = render_template_content(include_path, template_dir, project_name, (*seen, include_path))
        return f"{{# BEGIN inlined include {include_name} #}}\n{included}\n{{# END inlined include {include_name} #}}"

    content = INCLUDE_RE.sub(replace_include, content)
    content = content.replace("{{ TEMPLATE_PROJECT_NAME }}", project_name)
    return content


def template_payload(path: Path, project: dict[str, Any], template_dir: Path, composite: bool = False) -> dict[str, Any]:
    content = "" if composite else render_template_content(path, template_dir, project["name"], (path.resolve(),))
    metadata = parse_catc_metadata(content)
    return {
        "name": path.name,
        "description": "Synced from GitHub repository CatalystCenter-BGP-EVPN-VXLAN",
        "projectName": project["name"],
        "projectId": project["id"],
        "softwareType": metadata.get("softwareType", "IOS-XE"),
        "softwareVariant": metadata.get("softwareVariant", "XE"),
        "language": metadata.get("language", "JINJA"),
        "composite": composite,
        "deviceTypes": DEFAULT_DEVICE_TYPES,
        "templateContent": content,
        "templateParams": [],
        "rollbackTemplateParams": [],
    }


def content_changed(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    keys = ["name", "description", "softwareType", "softwareVariant", "language", "composite", "templateContent"]
    for key in keys:
        if current.get(key) != desired.get(key):
            return True
    current_series = sorted((d.get("productSeries", "") for d in current.get("deviceTypes", [])))
    desired_series = sorted((d.get("productSeries", "") for d in desired.get("deviceTypes", [])))
    return current_series != desired_series


def containing_changed(current: dict[str, Any], desired: list[dict[str, Any]]) -> bool:
    current_names = [item.get("name") for item in current.get("containingTemplates", [])]
    desired_names = [item.get("name") for item in desired]
    return current_names != desired_names


def composite_changed(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    if current.get("description", "") != desired.get("description", ""):
        return True
    if bool(current.get("composite")) != bool(desired.get("composite")):
        return True
    return containing_changed(current, desired.get("containingTemplates", []))


def project_template_index(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {template["name"]: template for template in project.get("templates", [])}


def refresh_project(client: CatalystCenter, project_name: str) -> dict[str, Any]:
    project = find_project(client, project_name)
    if not project:
        raise CatcError(f"Project {project_name!r} disappeared")
    return project


def wait_for_project_template(
    client: CatalystCenter,
    project_name: str,
    template_name: str,
    timeout: int = 90,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        project = refresh_project(client, project_name)
        template = project_template_index(project).get(template_name)
        if template:
            return project, template
        time.sleep(2)
    raise CatcError(f"Template {template_name!r} was synced but not returned in project {project_name!r}")


def get_template(client: CatalystCenter, template_id: str) -> dict[str, Any]:
    data = client.get(f"/dna/intent/api/v1/template-programmer/template/{template_id}")
    if not isinstance(data, dict):
        raise CatcError(f"Unexpected template response for {template_id}: {data!r}")
    return data


def has_template_errors(template_detail: dict[str, Any]) -> bool:
    validation = template_detail.get("validationErrors") or {}
    errors = list(validation.get("templateErrors") or []) + list(validation.get("rollbackTemplateErrors") or [])
    return any(error.get("type") != "POTENTIAL_CONFLICT" for error in errors)


def ensure_template(
    client: CatalystCenter,
    project: dict[str, Any],
    path: Path,
    dry_run: bool,
    commit: bool,
) -> tuple[dict[str, Any], bool]:
    desired = template_payload(path, project, path.parent)
    template_index = project_template_index(project)
    existing = template_index.get(path.name)
    changed = False
    if existing:
        current = get_template(client, existing["id"]) if not dry_run else existing
        if dry_run or content_changed(current, desired):
            print(f"Updating template: {path.name}")
            changed = True
            if not dry_run:
                desired["id"] = existing["id"]
                desired["parentTemplateId"] = current.get("parentTemplateId", existing["id"])
                wait_task(
                    client,
                    task_id_from(client.put("/dna/intent/api/v1/template-programmer/template", desired)),
                    f"update template {path.name}",
                )
        else:
            print(f"No change: {path.name}")
    else:
        print(f"Creating template: {path.name}")
        changed = True
        if not dry_run:
            wait_task(
                client,
                task_id_from(client.post(f"/dna/intent/api/v1/template-programmer/project/{project['id']}/template", desired)),
                f"create template {path.name}",
            )
    if dry_run:
        return {"id": f"dry-run-{path.name}", "name": path.name, "latestVersionTime": 1}, changed
    project, template = wait_for_project_template(client, project["name"], path.name)
    if commit and (changed or not template.get("latestVersionTime")):
        version_template(client, template["id"], f"GitHub sync for {path.name}")
    return template, changed


def version_template(client: CatalystCenter, template_id: str, comment: str) -> None:
    print(f"Versioning template: {template_id}")
    try:
        result = client.post(
            "/dna/intent/api/v1/template-programmer/template/version",
            {"templateId": template_id, "comments": comment},
        )
    except CatcError as exc:
        if exc.status == 400 and "No changes" in str(exc):
            return
        raise
    wait_task(client, task_id_from(result), f"version template {template_id}")


def parse_build_file(path: Path) -> tuple[str, list[str]]:
    composite_name: str | None = None
    names: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"composite_name:\s*['\"]?(?P<name>[^'\"]+)['\"]?$", line)
        if match:
            composite_name = match.group("name").strip()
            continue
        match = re.match(r"-\s*name:\s*['\"]?(?P<name>[^'\"]+)['\"]?$", line)
        if match:
            names.append(match.group("name").strip())
    return composite_name or f"{path.stem}.j2", names


def containing_template_payload(detail: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "id",
        "name",
        "description",
        "tags",
        "composite",
        "language",
        "projectName",
        "deviceTypes",
        "templateParams",
    ]
    return {field: detail[field] for field in fields if field in detail}


def ensure_composite(
    client: CatalystCenter,
    project: dict[str, Any],
    build_file: Path,
    dry_run: bool,
    commit: bool,
) -> None:
    composite_name, child_names = parse_build_file(build_file)
    template_index = project_template_index(refresh_project(client, project["name"])) if not dry_run else {}
    containing: list[dict[str, Any]] = []
    for child_name in child_names:
        child = template_index.get(child_name)
        if dry_run:
            containing.append({"id": f"dry-run-{child_name}", "name": child_name, "composite": False, "language": "JINJA"})
            continue
        if not child:
            raise CatcError(f"Composite child template {child_name!r} is missing from project {project['name']!r}")
        containing.append(containing_template_payload(get_template(client, child["id"])))

    desired = template_payload(Path(composite_name), project, build_file.parent, composite=True)
    desired["name"] = composite_name
    desired["description"] = f"Composite template generated from {build_file.name}"
    desired["containingTemplates"] = containing
    desired.pop("templateContent", None)

    existing = template_index.get(composite_name)
    changed = False
    if existing:
        current = get_template(client, existing["id"]) if not dry_run else existing
        if dry_run or composite_changed(current, desired):
            print(f"Updating composite template: {composite_name}")
            changed = True
            if not dry_run:
                desired["id"] = existing["id"]
                desired["parentTemplateId"] = current.get("parentTemplateId", existing["id"])
                wait_task(
                    client,
                    task_id_from(client.put("/dna/intent/api/v1/template-programmer/template", desired)),
                    f"update composite {composite_name}",
                )
        else:
            print(f"No change: {composite_name}")
    else:
        print(f"Creating composite template: {composite_name}")
        changed = True
        if not dry_run:
            wait_task(
                client,
                task_id_from(client.post(f"/dna/intent/api/v1/template-programmer/project/{project['id']}/template", desired)),
                f"create composite {composite_name}",
            )

    if dry_run:
        return
    project, composite = wait_for_project_template(client, project["name"], composite_name)
    if commit and (changed or not composite.get("latestVersionTime")):
        version_template(client, composite["id"], f"GitHub sync for {composite_name}")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync BGP EVPN Jinja templates into Catalyst Center.")
    parser.add_argument("--host", default=os.environ.get("CATC_HOST"), help="Catalyst Center host or IP")
    parser.add_argument("--username", default=os.environ.get("CATC_USERNAME"), help="Catalyst Center username")
    parser.add_argument("--password", default=os.environ.get("CATC_PASSWORD"), help="Catalyst Center password")
    parser.add_argument("--project-name", default=os.environ.get("CATC_PROJECT_NAME", DEFAULT_PROJECT_NAME))
    parser.add_argument("--template-dir", default=DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--build-file", default=DEFAULT_BUILD_FILE)
    parser.add_argument("--verify-tls", action="store_true", help="Verify Catalyst Center TLS certificate")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-commit", action="store_true", help="Create/update templates but do not version them")
    return parser.parse_args()


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")
    args = parse_args()
    if not args.host:
        print("CATC_HOST or --host is required", file=sys.stderr)
        return 2
    username = args.username or input("Catalyst Center username: ")
    password = args.password or getpass.getpass("Catalyst Center password: ")
    template_dir = (repo_root / args.template_dir).resolve()
    build_file = (repo_root / args.build_file).resolve()
    if not template_dir.is_dir():
        print(f"Template directory does not exist: {template_dir}", file=sys.stderr)
        return 2
    if not build_file.is_file():
        print(f"Composite build file does not exist: {build_file}", file=sys.stderr)
        return 2

    templates = sorted(template_dir.glob("*.j2"))
    if not templates:
        print(f"No .j2 templates found in {template_dir}", file=sys.stderr)
        return 2

    client = CatalystCenter(args.host, username, password, verify_tls=args.verify_tls, timeout=args.timeout)
    if args.dry_run:
        print(f"Dry run: would sync {len(templates)} templates to project {args.project_name!r}")
    else:
        client.authenticate()
    project = ensure_project(client, args.project_name, args.dry_run)
    changed_names: set[str] = set()
    for path in templates:
        _, changed = ensure_template(client, project, path, args.dry_run, commit=False)
        if changed:
            changed_names.add(path.name)
        if not args.dry_run:
            project = refresh_project(client, args.project_name)

    if not args.dry_run and not args.no_commit:
        project = refresh_project(client, args.project_name)
        template_index = project_template_index(project)
        version_order = sorted(templates, key=lambda item: (item.name.startswith("FABRIC-"), item.name))
        for path in version_order:
            template = template_index.get(path.name)
            if not template:
                raise CatcError(f"Template {path.name!r} was not found before versioning")
            detail = get_template(client, template["id"])
            if path.name in changed_names or not template.get("latestVersionTime") or has_template_errors(detail):
                version_template(client, template["id"], f"GitHub sync for {path.name}")

    ensure_composite(client, project, build_file, args.dry_run, commit=not args.no_commit)
    print(f"Sync complete. Changed child templates: {len(changed_names)}; project: {args.project_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
