# Agentic Home development

Continue authorized implementation and automatic verification without requiring milestone sign-offs. Hardware/account evidence is recorded truthfully and does not block software packaging. Runtime approval of exact household changes remains part of the product.

- Product scope (2026-10-11): outer Linux first. The main release is a bootable Linux installer plus a host bundle; Controller/Runner run outside a managed HAOS KVM VM. Do not collapse the product back to HAOS Apps. Keep shared runtime and versioned Linux/hardware profiles. See docs/LINUX_PRODUCT.md.
- Build with scripts/build_host.py; never execute host/install.py --apply on the development machine. Test installers against isolated virtual disks. No actual laptop disk was selected for erasure in this session.

- Use `pytest-disktmp uv run pytest -q ...`. Never put test fixtures, SQLite/Git scratch or basetemps on `/dev/shm` or other tmpfs. Use unique disk directories below `~/agent-work/tmp/pytest/` and remove task containers in `finally`.
- Keep Controller and Runner credentials separate. No Agent CLI or arbitrary shell execution in Controller. No HA management bearer in Runner jobs.
- Preserve strict intent → policy → exact proposal → owner approval → journal → backup → staging/readback → activation → evidence. Do not replace runtime checks with fixture success.
- Read `DESIGN.md` for UI changes; regenerate compiled `controller/static/` with `npm run build --prefix web`.
- Change Pydantic models first, then `uv run python scripts/contracts.py`; check with `--check`.
- Run the relevant behavior tests. Real platform tests use `--real-ha --containers` and require locally built App images. No real cloud account is required for these automated tests.
- Locks and `evidence/` distinguish actual execution from protocol fixtures. Do not silently change CLI versions, enable built-in tools, claim hardware verification, update the user's HAOS or push upstream repos.
- Rebuild `dist/` with `uv run python scripts/package.py`. Do not put credentials, `_upstream`, virtualenvs or node_modules in archives.
