# Notes for AI agents (Claude Code and others)

Setup Manager controls Linux "setup PCs" that share USB devices with USB/IP. See README.md for the parts:
the agent on each setup PC (`setup_manager/agent/`), the GTK app, the CLI and the MCP server on the
user's Ubuntu PC.

## "Apply the new fix" (or "update the lab", "do the rollout")

This means: update the user's PC and every setup PC to the code of this clone. Do this:

1. You must run on the user's Ubuntu PC (the PC with the Setup Manager app), in this folder, as the
   normal user. A cloud session cannot do it: it has no SSH access to the setup PCs. If you are not on
   that PC, tell the user to open Claude Code in this folder on it.
2. Optional: `./update-lab.sh --dry-run` shows the plan and changes nothing.
3. Run:

   ```bash
   ./update-lab.sh --yes
   ```

   It does `git pull`, updates the app (`install-app.sh`), then runs `setup-manager rollout --yes` for
   every setup PC. It asks nothing. It can take some minutes for each setup PC: use a long timeout.
4. Read the result and tell the user:
   - Exit code 0: all setup PCs are updated. Tell the user to close the app and open it again.
   - Exit code 2: some PCs failed. The lines with `FAILED` say why. Fix the cause (see below), then run
     the same command again. It continues: nothing is lost, the new tokens are kept.
   - Exit code 1: nothing was started. The output says why.

What to do with the usual messages:

| Message | What to do |
|---|---|
| `sudo on ... needs a password, and there is no terminal to ask it` | Do not guess a password, and do not write it into a file. Ask the user to run `./update-lab.sh` once in a terminal, or to allow sudo without a password on that setup PC, or to give the password for `SETUP_MANAGER_SUDO_PASSWORD` in this command only. |
| `SSH to USER@HOST failed` | SSH needs a key. Ask the user to run `ssh-copy-id USER@HOST` once, then run the command again. |
| `you have local changes ...: not pulling` | Do not stash or discard the user's changes. Tell the user, and ask what to do. |
| `updating the app without the sudo steps` | That is OK. Tell the user to run `./install-app.sh` in a terminal once (the sudo rule for usbip). |
| `unknown setup(s)` | The name is not in the config. `setup-manager list` shows the setups. |

`git pull` gets only the commits of the branch that this clone tracks. A fix is in the lab only after it
is on that branch.

## Code changes

- Tests: `python3 -m pytest -q` (about 4 minutes). Also run `python3 -m pyflakes setup_manager tests`
  (the 3 warnings about `STORE` in `cli.py` are known and harmless) and `bash -n` on the shell scripts.
- `setup_manager/agent/setup_agent.py` runs on the setup PCs: Python 3.8, standard library only.
- The GTK app (`setup_manager/app/`) cannot be imported without GTK; its test is skipped then.
- USB/IP modes of the agent (`"usbip_network"`): `open` (the default: `usbip` by hand works as without
  Setup Manager), `manual` (`--usbip-filter`) and `tunnel` (`--tunnel-only`). The default and the
  security behaviour are the user's decisions: do not change them without asking.
- After a code fix, the user applies it to the lab with "apply the new fix" (above).
