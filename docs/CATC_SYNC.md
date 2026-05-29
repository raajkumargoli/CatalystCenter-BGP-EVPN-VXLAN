# Catalyst Center Template Sync

This local copy is wired for Catalyst Center at `172.26.193.27` and syncs into the Template Editor project `BGP EVPN GitHub`.

## First-time setup

```bash
cd /Users/ragoli/Documents/WORK/EVPN/CatC_Github_EVPN/CatalystCenter-BGP-EVPN-VXLAN
cp .env.example .env
```

Edit `.env` and set `CATC_PASSWORD`. The `.env` file is ignored by Git.

## Push templates to Catalyst Center

```bash
python3 scripts/catc_template_sync.py
```

The script creates or updates every `.j2` file in `BGP EVPN/`, versions each template, and creates or updates the `BGP-EVPN-BUILD.j2` composite template using the order in `BGP EVPN/BGP-EVPN-BUILD.yml`.

The source files stay modular in Git. During upload, the script expands `DEFN-*` and `FUNC-*` includes into each runtime `FABRIC-*` template because this Catalyst Center instance does not reliably resolve project-relative include paths during template versioning.

## Provision from the UI

1. Go to `Tools > Template Editor`.
2. Open project `BGP EVPN GitHub`.
3. Confirm `BGP-EVPN-BUILD.j2` exists and is versioned.
4. Go to `Design > Network Profiles`.
5. Create or edit a CLI switching profile for the target site and attach `BGP-EVPN-BUILD.j2`.
6. Go to `Provision > Inventory`, select the intended fabric devices, and provision from the UI.

The local role mappings currently target:

| Fabric role | Catalyst Center hostnames |
| --- | --- |
| Spines / RRs | `CLSJ-Spine1`, `CLSJ-Spine2` |
| Leaves | `CLSJ03-Leaf1`, `CLSJ03-Leaf2`, `CLSJ03-Leaf3`, `CLSJ22-Leaf4`, `CLSJ22-Leaf5`, `CLSJ22-Leaf6` |
| Borders | `CLSJ-BDR-1`, `CLSJ-BDR-2` |

## Update VLANs, subnets, or host roles

Edit the `DEFN-*.j2` data files, then rerun the sync script.

Common files:

- `BGP EVPN/DEFN-OVERLAY.j2`: tenant VLANs, SVI IPs, DHCP helper addresses, BUM multicast groups, and per-VLAN network statements.
- `BGP EVPN/DEFN-VRF.j2`: VRF IDs and which VRFs are instantiated on each node.
- `BGP EVPN/DEFN-L3OUT.j2`: L3OUT spine uplinks, peer IPs, neighbor ASN, and red VRF aggregates.
- `BGP EVPN/DEFN-CLIENT-PORTS.j2`: leaf access ports and VLAN assignments.
- `BGP EVPN/DEFN-LOOPBACKS.j2`: underlay loopback addresses and overlay loopback prefixes.
- `BGP EVPN/DEFN-ROLES.j2`: node roles by Catalyst Center inventory hostname.

After editing:

```bash
git diff
python3 scripts/catc_template_sync.py
git add 'BGP EVPN' scripts docs .gitignore .env.example
git commit -m "Update BGP EVPN Catalyst Center templates"
git push
```

## Pull future GitHub changes

```bash
git pull --rebase
python3 scripts/catc_template_sync.py
```

If upstream changes conflict with local site-specific values, keep the local Catalyst Center hostnames and subnet plan, then rerun the sync.
