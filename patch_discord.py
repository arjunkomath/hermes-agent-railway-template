"""
Patch hermes-agent Discord platform to handle command sync failures gracefully.
Discord enforces an 8000-byte limit on command group total size; the 'skill'
group can exceed this, causing CommandSyncFailure that crashes the gateway.

Patches applied:
  1. Wrap tree.sync() in try/except so the gateway keeps running even when
     command sync fails.
  2. Truncate .description attribute accesses to [:100] so no single
     subcommand description blows past Discord's per-field limit.
"""
import re
import sys
import importlib.util
from pathlib import Path


def find_file() -> Path | None:
    try:
        spec = importlib.util.find_spec("gateway.platforms.discord")
        if spec and spec.origin:
            p = Path(spec.origin)
            if p.exists():
                return p
    except Exception:
        pass
    for candidate in [
        "/tmp/hermes-agent/gateway/platforms/discord.py",
        "/hermes-agent/gateway/platforms/discord.py",
    ]:
        p = Path(candidate)
        if p.exists():
            return p
    return None


def patch(f: Path) -> bool:
    src = f.read_text()
    patched = src

    # 1. Wrap tree.sync() calls in try/except to survive oversized command groups
    sync_re = re.compile(
        r"^( *)(await\s+(?:[\w.]+\.)?tree\.sync\(.*?\))",
        re.MULTILINE,
    )
    if sync_re.search(patched):
        def wrap_sync(m: re.Match) -> str:
            indent, stmt = m.group(1), m.group(2)
            return (
                f"{indent}try:\n"
                f"{indent}    {stmt}\n"
                f"{indent}except Exception as _sync_err:\n"
                f'{indent}    print(f"[hermes] Discord command sync failed (non-fatal): {{_sync_err}}", flush=True)'
            )
        patched = sync_re.sub(wrap_sync, patched)
        print(f"  [1] Wrapped tree.sync() with try/except")

    # 2. Truncate dynamic description attributes to stay within Discord limits
    desc_re = re.compile(r"(description\s*=\s*)([\w.]+\.description)\b")
    if desc_re.search(patched):
        patched = desc_re.sub(r"\1(\2 or '')[:100]", patched)
        print(f"  [2] Added [:100] truncation on .description attributes")

    if patched != src:
        f.write_text(patched)
        return True

    print("  No patch points matched — file structure may differ from expected")
    return False


if __name__ == "__main__":
    f = find_file()
    if not f:
        print("gateway/platforms/discord.py not found — skipping Discord patch")
        sys.exit(0)
    print(f"Patching {f} …")
    ok = patch(f)
    print("Discord patch applied." if ok else "Discord patch was a no-op.")
