# masterpilot-04 (pilot 88004, SMB)

The same two parts as the 88002 and 88003 folders:

| Folder | Pattern | What it does |
|---|---|---|
| `SMB_AUTOMATION/` | MASTERPILOT-02 `MDS_AUTOMATION` | One `create_smb_<flow>_user.py` per email. Each creates a new SMB launchpad user, loads its data, sets its user configs, waits for the email and checks it against the platform APIs. It is self-contained (`smb_common.py`, `smb_flows.py`, `data/`) and runs on any env via `targets/<env>.json`. |
| `cdg_user_setup/` | masterpilot-03 `cdg_user_setup` | Ports persona users from a source environment into 88004 through DETO/CDG (`create_users.py`, `check_users.py`, `fetch_ported_uuids.py`, `check_valid_fuels.py`). The code is unchanged from masterpilot-03; `config.json.example` and `sources.csv.example` hold the 88004 values. |
| `rate_migration/` | masterpilot-03 `rate_migration` | Copies rate plans into 88004 (`migrate_rate_configuration.py`). The example ports plans 960-963 from productqa 88004 to another env. |

```bash
python3 masterpilot-04/SMB_AUTOMATION/run_all.py list
python3 masterpilot-04/SMB_AUTOMATION/create_smb_welcome_user.py --env productqa
cp masterpilot-04/cdg_user_setup/config.json.example masterpilot-04/cdg_user_setup/config.json   # then edit
uv run python masterpilot-04/cdg_user_setup/create_users.py
```

`config.json`, `sources.csv`, `.env` and `results/` are git-ignored because they hold tokens or QA identities.
