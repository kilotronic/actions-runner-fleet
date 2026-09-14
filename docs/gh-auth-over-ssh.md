# gh auth over SSH (macOS hosts)

## Symptom

SSH into a Mac host, run `gh` (or `gh auth status`), and it reports _"Failed to
log in"_ / not authenticated — even though the same account works in a local
terminal on that Mac. Re-running `gh auth login` over SSH "fixes" it briefly,
then it breaks again on the next SSH session. Across several Macs it looks like
gh auth keeps getting clobbered.

## Cause (it isn't a clobber)

On macOS, gh stores its OAuth token in the **login keychain**, not in a file —
`~/.config/gh/hosts.yml` holds only the account list and active user, with no
`oauth_token:` line. The login keychain is unlocked by the **GUI login** and is
**locked in any non-GUI session** (SSH, or before anyone has logged in). Over
SSH, gh cannot read its own token and reports a failed login.

The apparent clobber is a loop: each `gh auth login` over SSH rewrites
`hosts.yml` to a fresh minimal state and stows the token back in the
still-unreadable keychain, so the next SSH session cannot read it either. GitHub
is not invalidating the token; the keychain is simply invisible over SSH.

## Fix: file storage

Store the token in `hosts.yml` (mode `0600`) instead of the keychain, so any SSH
session can read it without a prompt. The existing token can be reused:

```bash
# In a GUI session on the host (keychain readable), or pipe the token in over
# SSH from a machine that can already read it:
gh auth token -u <account> | gh auth login --with-token --insecure-storage
```

Verify over SSH: `gh auth status` should show the source as `(…/hosts.yml)`, not
`(keyring)`, and `gh api user -q .login` should print the account. `oauth_token`
appearing twice in `hosts.yml` is normal gh format (host-level and per-user).

### Trade-off

The token now sits in a file readable by your user. That is a reasonable trade
on a runner host when:

- the disk is encrypted at rest (FileVault on) — at-rest encryption is the
  keychain's main advantage anyway; and
- `~/.config` is not synced or backed up anywhere off the host.

The alternative, `security unlock-keychain` in every SSH session, needs the macOS
login password each time — and automating it means storing that password, a
worse secret to expose than a gh token.

## Scope

- **Interactive SSH administration only.** The runners are LaunchAgents that run
  inside the GUI login session, so they can reach the keychain. Workflows that
  need `gh` in CI should set `GH_TOKEN` explicitly rather than depend on either.
- **Linux hosts are unaffected.** Headless gh with no Secret Service already
  stores the token in `hosts.yml`.
- Over SSH, a Homebrew gh lives in `/opt/homebrew/bin`, which is not on the
  default non-interactive PATH — prefix commands with
  `PATH=/opt/homebrew/bin:$PATH`.

## It regresses — check the storage, not just the ✓

File storage is not sticky. Any later `gh auth login` or `gh auth refresh` run
without `--insecure-storage` silently moves the token back into the keychain,
and the SSH breakage returns.

The tell is the **source** that `gh auth status` shows, not its ✓ or ✗:

```bash
gh auth status   # want: (…/.config/gh/hosts.yml)   bad: (keyring)
```

To confirm the session really cannot reach the keychain, as opposed to a revoked
token (which needs a different fix):

```bash
security show-keychain-info ~/Library/Keychains/login.keychain-db
# "User interaction is not allowed."  => locked keychain; the token is probably fine
gh auth token   # "no oauth token found" is what a locked keychain looks like
```

`gh auth status` reports a locked keychain as an invalid login, which reads like
a revoked token — it isn't. Strip ambient tokens when verifying, or you will test
the wrong credential: `env -u GH_TOKEN -u GITHUB_TOKEN gh api user -q .login`.
