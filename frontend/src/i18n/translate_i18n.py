#!/usr/bin/env python3
"""Standalone i18n translation filler.

Self-contained, stdlib-only. Reads `en` as the source of truth, finds keys
missing (or still equal to English) in every other locale, then asks a chosen
LLM provider to translate them and merges the result back.

This tool deliberately has NO dependency on the tessallite codebase. It only
reads API-key VALUES from the environment and from .env files; it never imports
project code. HTTP is done by shelling out to `curl`.

Run:  python translate_i18n.py            (interactive, full run)
      python translate_i18n.py --dry-run  (diff + missing-keys reports only)
      python translate_i18n.py --provider zai --model glm-4.6 --yes

Re-run it whenever `en` gains new keys; each run only works the keys that are
absent or still equal to English, so repeated runs converge to zero.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Human-readable language names for the translation prompt.
LANG_NAMES = {
    "ar": "Arabic",
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "ja": "Japanese",
    "pt": "Portuguese (European)",
    "zh": "Chinese (Simplified)",
}

# Provider registry. `type` selects the request shape. `base` and the model
# defaults are suggestions only — the user can override the model at the prompt
# or with --model. Model ids drift over time; prefer an env-supplied model
# (model_env) or just type the current one.
PROVIDERS = {
    "anthropic": {
        "env_keys": ["ANTHROPIC_API_KEY"], "type": "anthropic",
        "base": "https://api.anthropic.com", "default_model": "claude-opus-4-8",
        "models": ["claude-opus-4-8", "claude-sonnet-4-6",
                   "claude-haiku-4-5-20251001"],
    },
    "openai": {
        "env_keys": ["OPENAI_API_KEY"], "type": "openai",
        "base": "https://api.openai.com/v1", "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    },
    "google": {
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "type": "google",
        "base": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
    },
    "zai": {
        "env_keys": ["ZAI_API_KEY"], "type": "openai",
        "base": "https://api.z.ai/api/paas/v4",
        "model_env": "ZAI_MODEL", "default_model": "glm-4.6",
        "models": ["glm-4.6", "glm-4.5", "glm-4.5-air"],
    },
    "deepseek": {
        "env_keys": ["DEEPSEEK_API_KEY"], "type": "openai",
        "base": "https://api.deepseek.com", "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "groq": {
        "env_keys": ["GROQ_API_KEY"], "type": "openai",
        "base": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    },
    "mistral": {
        "env_keys": ["MISTRAL_API_KEY"], "type": "openai",
        "base": "https://api.mistral.ai/v1", "default_model": "mistral-large-latest",
        "models": ["mistral-large-latest", "mistral-small-latest"],
    },
    "openrouter": {
        "env_keys": ["OPENROUTER_API_KEY"], "type": "openai",
        "base": "https://openrouter.ai/api/v1", "default_model": "openai/gpt-4o-mini",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4-6",
                   "google/gemini-2.5-flash"],
    },
}

PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}|\{[^{}]*\}")
NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)*(?![\w.])")
PROTECTED_TERMS = {
    "Tessallite",
    "API",
    "BI",
    "CSV",
    "DAX",
    "HTML",
    "HTTP",
    "HTTPS",
    "ID",
    "JSON",
    "JWT",
    "KPI",
    "LLM",
    "MDX",
    "OAuth",
    "OIDC",
    "RBAC",
    "REST",
    "SAML",
    "SQL",
    "SSO",
    "TSV",
    "URL",
    "XMLA",
}


# --------------------------------------------------------------------------- #
# Environment / key discovery
# --------------------------------------------------------------------------- #
def load_env(root: Path) -> dict:
    """OS env, then fill (without overriding) from local + tessallite .env."""
    env = dict(os.environ)
    candidates = [SCRIPT_DIR / ".env", root / ".env"]
    for parent in [root, *root.parents]:
        if parent.name == "tessallite":
            candidates.append(parent / ".env")
    seen = set()
    for path in candidates:
        rp = path.resolve()
        if rp in seen or not path.is_file():
            continue
        seen.add(rp)
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in env:
                env[key] = val
    return env


def detect_providers(env: dict) -> dict:
    """Return {provider_id: api_key} for every provider with a key present."""
    found = {}
    for pid, spec in PROVIDERS.items():
        for ekey in spec["env_keys"]:
            if env.get(ekey):
                found[pid] = env[ekey]
                break
    return found


# --------------------------------------------------------------------------- #
# Locale diffing
# --------------------------------------------------------------------------- #
def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: {path} is not valid JSON: {exc}")


def write_json(path: Path, data: dict) -> None:
    """Atomic write, UTF-8, non-ASCII left readable to match existing files."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def load_source(root: Path, source: str) -> dict:
    """{filename: {key: value}} for every JSON file in the source locale."""
    src_dir = root / source
    if not src_dir.is_dir():
        sys.exit(f"ERROR: source locale dir not found: {src_dir}")
    out = {}
    for jf in sorted(src_dir.glob("*.json")):
        out[jf.name] = read_json(jf)
    if not out:
        sys.exit(f"ERROR: no JSON files in {src_dir}")
    return out


def detect_targets(root: Path, source: str) -> list:
    targets = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if d.name == source:
            continue
        if any(d.glob("*.json")):
            targets.append(d.name)
    return targets


def find_rules_file(root: Path, locale: str) -> Path | None:
    """Locale rules .md; filename casing is inconsistent, so glob loosely."""
    matches = glob.glob(str(root / locale / "*[lL]ocalization*[rR]ules.md"))
    return Path(matches[0]) if matches else None


def diff_locale(source: dict, root: Path, locale: str) -> dict:
    """{filename: {key: english_value}} for keys needing translation.

    A key needs work if it is absent in the target OR its value still equals
    the English value.
    """
    missing = {}
    for fname, src_map in source.items():
        tgt_map = read_json(root / locale / fname)
        todo = {}
        for k, v in src_map.items():
            if k not in tgt_map:
                todo[k] = v
            elif tgt_map[k] == v and not intentional_identical_ok(v):
                todo[k] = v
        if todo:
            missing[fname] = todo
    return missing


def render_report(locale: str, missing: dict) -> str:
    total = sum(len(v) for v in missing.values())
    lines = [
        f"# Missing / untranslated keys — {locale} ({LANG_NAMES.get(locale, locale)})",
        "",
        f"Generated by translate-i18n. {total} key(s) across {len(missing)} file(s).",
        "Throwaway artifact — safe to delete; regenerated on every run.",
        "",
    ]
    for fname in sorted(missing):
        lines.append(f"## {fname}")
        lines.append("")
        for key, val in missing[fname].items():
            flat = " ".join(str(val).split())
            lines.append(f"- `{key}` — {flat}")
        lines.append("")
    return "\n".join(lines)


def write_report(root: Path, locale: str, missing: dict) -> None:
    path = root / locale / "missing-keys.md"
    path.write_text(render_report(locale, missing), encoding="utf-8")


# --------------------------------------------------------------------------- #
# LLM JSON parsing / cleaning
# --------------------------------------------------------------------------- #
def _loads_relaxed(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            chunk = re.sub(r",\s*([}\]])", r"\1", text[i:j + 1])
            return json.loads(chunk)
        raise


def clean_value(s: str) -> str:
    """Collapse stray escape artifacts that survive JSON decoding."""
    if "\\" not in s:
        return s
    s = s.replace("\\\\", "\x00")  # protect genuine backslashes
    s = (s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
           .replace('\\"', '"').replace("\\'", "'").replace("\\/", "/"))
    return s.replace("\x00", "\\")


def parse_llm_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t.strip())
    obj = _loads_relaxed(t.strip())
    if isinstance(obj, str):          # double-encoded JSON string
        obj = _loads_relaxed(obj)
    if not isinstance(obj, dict):
        raise ValueError("LLM response was not a JSON object")
    return {k: clean_value(v) for k, v in obj.items() if isinstance(v, str)}


def placeholders_ok(src: str, dst: str) -> bool:
    return Counter(PLACEHOLDER_RE.findall(src)) == Counter(PLACEHOLDER_RE.findall(dst))


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_-]*", text)


def intentional_identical_ok(src: str) -> bool:
    """True when a target matching English is likely intentional.

    Brand names, acronyms, protocols, and numeric/version labels often do not
    translate. Plain UI prose such as "Save" should still be sent for
    translation when it equals English.
    """
    stripped = PLACEHOLDER_RE.sub(" ", src)
    words = _words(stripped)
    if not words:
        return True
    return all(w in PROTECTED_TERMS or w.upper() == w for w in words)


def protected_tokens_ok(src: str, dst: str) -> bool:
    """Validate preservation of brand/protocol tokens and numeric literals."""
    for term in PROTECTED_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", src) and not re.search(
            rf"\b{re.escape(term)}\b", dst
        ):
            return False
    return Counter(NUMBER_RE.findall(src)) == Counter(NUMBER_RE.findall(dst))


# --------------------------------------------------------------------------- #
# LLM call (via curl)
# --------------------------------------------------------------------------- #
def build_request(provider: str, model: str, api_key: str, base: str,
                  system: str, user: str, max_tokens: int):
    """Return (url, headers, payload-dict) for the chosen provider type."""
    spec = PROVIDERS[provider]
    ptype = spec["type"]
    if ptype == "anthropic":
        return (
            f"{base}/v1/messages",
            ["content-type: application/json",
             f"x-api-key: {api_key}",
             "anthropic-version: 2023-06-01"],
            {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
             "system": system,
             "messages": [{"role": "user", "content": user}]},
        )
    if ptype == "google":
        return (
            f"{base}/models/{model}:generateContent?key={api_key}",
            ["content-type: application/json"],
            {"systemInstruction": {"parts": [{"text": system}]},
             "contents": [{"role": "user", "parts": [{"text": user}]}],
             "generationConfig": {"temperature": 0.2,
                                  "maxOutputTokens": max_tokens,
                                  "responseMimeType": "application/json"}},
        )
    # openai-compatible (openai, zai, deepseek, groq, mistral, openrouter)
    return (
        f"{base}/chat/completions",
        ["content-type: application/json", f"authorization: Bearer {api_key}"],
        {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]},
    )


def extract_text(provider: str, resp: dict) -> str:
    ptype = PROVIDERS[provider]["type"]
    if ptype == "anthropic":
        return "".join(b.get("text", "") for b in resp.get("content", []))
    if ptype == "google":
        cands = resp.get("candidates", [])
        if not cands:
            raise ValueError(f"no candidates in response: {resp}")
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    choices = resp.get("choices", [])
    if not choices:
        raise ValueError(f"no choices in response: {resp}")
    return choices[0].get("message", {}).get("content", "")


def call_llm(url: str, headers: list, payload: dict, timeout: int) -> dict:
    cmd = ["curl", "-sS", "-X", "POST", url, "--max-time", str(timeout)]
    for h in headers:
        cmd += ["-H", h]
    cmd += ["--data-binary", "@-"]
    proc = subprocess.run(
        cmd, input=json.dumps(payload).encode("utf-8"),
        capture_output=True, timeout=timeout + 15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}): "
                           f"{proc.stderr.decode('utf-8', 'ignore')[:500]}")
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"non-JSON HTTP response: {proc.stdout.decode('utf-8', 'ignore')[:500]}"
        ) from exc


# --------------------------------------------------------------------------- #
# Translation orchestration
# --------------------------------------------------------------------------- #
def system_prompt(locale: str, rules_text: str) -> str:
    lang = LANG_NAMES.get(locale, locale)
    return (
        f"{rules_text}\n\n"
        f"---\n"
        f"You are a professional software localizer translating UI strings "
        f"from English to {lang}. Follow ALL the localization rules above.\n"
        f"You receive a JSON object mapping message keys to English text. "
        f"Return ONLY a JSON object mapping the SAME keys to the translated "
        f"{lang} text.\n"
        f"- Return every key exactly as given; never add, drop, or rename keys.\n"
        f"- Preserve placeholders such as {{{{name}}}} or {{count}} verbatim "
        f"and in natural position.\n"
        f"- Keep technical terms, acronyms, URLs, and brand names that have no "
        f"common {lang} form unchanged.\n"
        f"- Output must be valid JSON and nothing else: no markdown fences, no "
        f"commentary, no escape artifacts."
    )


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def translate_file(provider, model, api_key, base, locale, fname, todo,
                   chunk_size, max_tokens, timeout, tmp_dir) -> tuple:
    """Translate one file's missing keys. Returns (translations, failed_keys)."""
    rules_path = find_rules_file(SCRIPT_DIR, locale)
    rules_text = rules_path.read_text(encoding="utf-8") if rules_path else \
        f"Translate the UI strings into {LANG_NAMES.get(locale, locale)}."
    system = system_prompt(locale, rules_text)

    translations, failed = {}, []
    keys = list(todo)
    for ci, batch in enumerate(chunked(keys, chunk_size)):
        pending = {k: todo[k] for k in batch}
        for attempt in range(1, 4):
            user = json.dumps(pending, ensure_ascii=False, indent=2)
            url, headers, payload = build_request(
                provider, model, api_key, base, system, user, max_tokens)
            try:
                resp = call_llm(url, headers, payload, timeout)
                raw = extract_text(provider, resp)
                (tmp_dir / f"{locale}__{fname}__chunk{ci}.txt").write_text(
                    raw, encoding="utf-8")
                got = parse_llm_json(raw)
            except Exception as exc:  # noqa: BLE001 — report and retry
                print(f"      chunk {ci} attempt {attempt} failed: {exc}")
                if attempt == 3:
                    failed.extend(pending)
                continue
            # Validate: keep good keys, retry the rest.
            still = {}
            for k, src in pending.items():
                dst = got.get(k)
                if (
                    isinstance(dst, str)
                    and dst
                    and (dst != src or intentional_identical_ok(src))
                    and placeholders_ok(src, dst)
                    and protected_tokens_ok(src, dst)
                ):
                    translations[k] = dst
                else:
                    still[k] = src
            if not still:
                break
            pending = still
            if attempt == 3:
                failed.extend(pending)
    return translations, failed


def merge_target(root, source_map, locale, fname, translations) -> int:
    """Write source-ordered target keys only.

    English is the structural authority. Target-only keys are stale after their
    English source key is removed, so retaining them makes every later pipeline
    run preserve catalogue drift indefinitely.
    """
    path = root / locale / fname
    existing = read_json(path)
    merged, added = {}, 0
    for key, en_val in source_map.items():
        if key in translations:
            merged[key] = translations[key]
            added += 1
        elif key in existing:
            merged[key] = existing[key]
        else:
            merged[key] = en_val  # fallback so the key exists; retried next run
    write_json(path, merged)
    return added


def merge_all_targets(root, source, targets, translations_by_locale) -> int:
    """Rewrite every source-backed target file in source order.

    Translation work is optional here: files with no new translations are still
    merged so stale target-only keys are pruned deterministically.
    """
    total_added = 0
    for locale in targets:
        locale_translations = translations_by_locale.get(locale, {})
        for fname, source_map in source.items():
            total_added += merge_target(
                root,
                source_map,
                locale,
                fname,
                locale_translations.get(fname, {}),
            )
    return total_added


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def choose_provider_and_key(available: dict) -> tuple:
    """Pick a detected provider, or manually choose any provider + paste a key."""
    ids = list(available)
    print("\nLLM providers:")
    for i, pid in enumerate(ids, 1):
        print(f"  {i}) {pid}  (key found in environment)")
    manual = len(ids) + 1
    print(f"  {manual}) manual entry (choose provider + paste API key)")
    while True:
        sel = input("Choose [number]: ").strip()
        if sel.isdigit():
            n = int(sel)
            if 1 <= n <= len(ids):
                pid = ids[n - 1]
                return pid, available[pid]
            if n == manual:
                return _manual_entry()
        print("  invalid choice")


def _manual_entry() -> tuple:
    pids = list(PROVIDERS)
    print("\nProviders:")
    for i, pid in enumerate(pids, 1):
        print(f"  {i}) {pid}")
    while True:
        sel = input("Choose provider [number]: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(pids):
            provider = pids[int(sel) - 1]
            break
        print("  invalid choice")
    while True:
        key = input(f"Paste API key for {provider}: ").strip()
        if key:
            return provider, key
        print("  key cannot be empty")


def choose_model(provider: str, env: dict) -> str:
    """Pick a suggested model, hit Enter for the default, or type a custom id."""
    spec = PROVIDERS[provider]
    default = env.get(spec.get("model_env", ""), "") or spec["default_model"]
    options = [default] + [m for m in spec.get("models", []) if m != default]
    print(f"\nModels for {provider}:")
    for i, m in enumerate(options, 1):
        print(f"  {i}) {m}{'  (default)' if i == 1 else ''}")
    manual = len(options) + 1
    print(f"  {manual}) manual entry (type a model id)")
    while True:
        sel = input(
            "Choose model [number, or type a model id, Enter for default]: "
        ).strip()
        if sel == "":
            return default
        if sel.isdigit():  # digits are menu selections
            n = int(sel)
            if 1 <= n <= len(options):
                return options[n - 1]
            if n == manual:
                while True:
                    m = input("Type model id: ").strip()
                    if m:
                        return m
                    print("  model id cannot be empty")
            print("  invalid choice")
            continue
        return sel  # any non-numeric input is taken as a literal model id


def main() -> int:
    ap = argparse.ArgumentParser(description="Fill missing i18n translations.")
    ap.add_argument("--root", default=str(SCRIPT_DIR),
                    help="i18n root (default: this script's dir)")
    ap.add_argument("--source", default="en", help="source locale (default en)")
    ap.add_argument("--targets", default="",
                    help="comma-separated target locales (default: auto-detect)")
    ap.add_argument("--provider", default="", help="provider id (skip menu)")
    ap.add_argument("--model", default="", help="model name (skip prompt)")
    ap.add_argument("--api-key", default="",
                    help="API key for --provider (overrides env detection)")
    ap.add_argument("--yes", action="store_true",
                    help="non-interactive; requires --provider")
    ap.add_argument("--dry-run", action="store_true",
                    help="diff + reports only; no LLM calls, no writes")
    ap.add_argument("--chunk-size", type=int, default=40)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    env = load_env(root)
    source = load_source(root, args.source)
    targets = ([t.strip() for t in args.targets.split(",") if t.strip()]
               or detect_targets(root, args.source))
    print(f"Source: {args.source} ({len(source)} files). "
          f"Targets: {', '.join(targets)}")

    # Phase 1 — diff + reports.
    work = {}
    for locale in targets:
        missing = diff_locale(source, root, locale)
        total = sum(len(v) for v in missing.values())
        if args.dry_run:
            print(f"  {locale}: {total} key(s) to translate "
                  f"across {len(missing)} file(s)")
            if missing:
                print(render_report(locale, missing))
        else:
            write_report(root, locale, missing)
            print(f"  {locale}: {total} key(s) to translate "
                  f"across {len(missing)} file(s) -> {locale}/missing-keys.md")
        if missing:
            work[locale] = missing

    if args.dry_run:
        print("\nDry run: no files written, no translation performed.")
        return 0

    # Phase 2 — provider/model selection, only when missing keys need LLM work.
    if work:
        available = detect_providers(env)
        if args.provider:
            if args.provider not in PROVIDERS:
                print(f"ERROR: unknown provider '{args.provider}'. "
                      f"Valid: {', '.join(PROVIDERS)}")
                return 1
            provider = args.provider
            api_key = args.api_key or available.get(provider)
            if not api_key:
                if args.yes:
                    print(f"ERROR: no API key for '{provider}'. Pass --api-key or "
                          f"set {'/'.join(PROVIDERS[provider]['env_keys'])}.")
                    return 1
                api_key = input(f"API key for {provider}: ").strip()
                if not api_key:
                    print("ERROR: no API key entered.")
                    return 1
        elif args.yes:
            print("ERROR: --yes requires --provider")
            return 1
        else:
            provider, api_key = choose_provider_and_key(available)
        model = args.model or (PROVIDERS[provider].get("default_model")
                               if args.yes else choose_model(provider, env))
        base = PROVIDERS[provider]["base"]
        print(f"\nUsing {provider} / {model}")
        tmp_dir = root / ".i18n-translate-tmp"
        tmp_dir.mkdir(exist_ok=True)
    else:
        provider = model = api_key = base = None
        tmp_dir = None
        print("\nNo missing translations. Normalising target catalog structure.")

    # Phase 3 — translate missing keys, then merge every source-backed file.
    translations_by_locale = {}
    grand_added, grand_failed = 0, 0
    for locale, missing in work.items():
        print(f"\n[{locale}] {LANG_NAMES.get(locale, locale)}")
        translations_by_locale[locale] = {}
        for fname, todo in missing.items():
            print(f"  {fname}: {len(todo)} key(s)")
            translations, failed = translate_file(
                provider, model, api_key, base, locale, fname, todo,
                args.chunk_size, args.max_tokens, args.timeout, tmp_dir)
            translations_by_locale[locale][fname] = translations
            grand_failed += len(failed)
            note = f" ({len(failed)} unresolved, left as English)" if failed else ""
            print(f"    translated {len(translations)} key(s){note}")

    grand_added = merge_all_targets(root, source, targets, translations_by_locale)

    raw_note = (f" Raw LLM output kept in {tmp_dir.name}/" if tmp_dir else "")
    print(f"\nDone. {grand_added} key(s) translated; "
          f"{grand_failed} left unresolved (re-run to retry).{raw_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
