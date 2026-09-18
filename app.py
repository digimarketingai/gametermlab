"""
GameTermLab | 遊戲術語擷取工具

Source-backed game terminology extraction.

Live connectors:
- League of Legends: Riot Data Dragon
- Pokémon franchise: PokéAPI community dataset

Other games:
- User-supplied TXT, CSV, and JSON

Important:
- English NLP only.
- Structured terms are distinguished from NLP candidates.
- No generated dialogue or invented terminology.
- Source offsets refer to normalized field text, not raw file bytes.
- Upload authenticity is declared by the user, not verified by the app.
- This is a local research prototype, not a multi-tenant service.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
import zipfile

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import gradio as gr
import pandas as pd
import requests_cache
import spacy

from bs4 import BeautifulSoup


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

APP_NAME = "GameTermLab"

GAMES = [
    "League of Legends — Riot Data Dragon",
    "Pokémon franchise — PokéAPI",
    "Other game — upload your sources",
]

CATEGORIES = [
    "Character",
    "Creature",
    "Move",
    "Place",
    "Item",
    "Term",
    "Phrase",
    "Clause",
    "Expression",
]

STRUCTURED_CATEGORIES = {
    "character": "Character",
    "creature": "Creature",
    "move": "Move",
    "place": "Place",
    "item": "Item",
    "term": "Term",
}

TEXT_CATEGORIES = {"Phrase", "Clause", "Expression"}

MAX_FILES = 10
MAX_FILE_BYTES = 1_000_000
MAX_FIELD_CHARS = 6000
MAX_CORPUS_CHARS = 150_000
MAX_SOURCE_FIELDS = 3000
MAX_OCCURRENCES = 30_000

CACHE_DIR = Path.home() / ".gametermlab"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HTTP = requests_cache.CachedSession(
    str(CACHE_DIR / "public_api_cache"),
    backend="sqlite",
    expire_after=3600,
    allowable_codes=(200,),
)

HTTP.headers.update({
    "User-Agent": "GameTermLab/1.0 source-backed-term-research",
    "Accept": "application/json",
})

NLP = None

OCCURRENCE_COLUMNS = [
    "game",
    "category",
    "term",
    "method",
    "review_status",
    "source_id",
    "source_title",
    "source_location",
    "source_type",
    "source_field",
    "version",
    "speaker",
    "start",
    "end",
    "context",
]

SUMMARY_COLUMNS = [
    "game",
    "category",
    "term",
    "extracted_mentions",
    "source_fields",
    "methods",
    "review_status",
]


# ------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text):
    """Whitespace normalization; no generated wording."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_html(text):
    """Convert API HTML descriptions to normalized visible text."""
    soup = BeautifulSoup(str(text or ""), "html.parser")
    return normalize(soup.get_text(" "))


def pointer_token(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def load_nlp():
    global NLP

    if NLP is None:
        try:
            NLP = spacy.load("en_core_web_sm")
        except OSError as exc:
            raise RuntimeError(
                "English NLP model missing. Run: "
                "python -m spacy download en_core_web_sm"
            ) from exc

    return NLP


def spreadsheet_safe(value):
    """
    Protect CSV/XLSX cells from common formula injection patterns.
    JSON exports retain the original extracted strings.
    """
    if not isinstance(value, str):
        return value

    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)

    if value.lstrip().startswith(("=", "+", "-", "@")):
        value = "'" + value

    return value


def safe_dataframe(frame):
    return frame.apply(
        lambda column: column.map(spreadsheet_safe)
    )


# ------------------------------------------------------------
# Controlled public API access
# ------------------------------------------------------------

def get_json(url):
    """
    Only the two built-in public data hosts are allowed.
    User-supplied URLs are not fetched.
    Redirects are intentionally not followed.
    """
    parsed = urlparse(url)

    allowed = {
        "pokeapi.co",
        "ddragon.leagueoflegends.com",
    }

    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Blocked API address.")

    response = HTTP.get(
        url,
        timeout=(10, 40),
        allow_redirects=False,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"HTTP {response.status_code} from {parsed.hostname}. "
            "The connector stopped; no automatic retry was attempted."
        )

    cached = bool(getattr(response, "from_cache", False))

    if not cached:
        time.sleep(0.3)

    return response.json(), cached


# ------------------------------------------------------------
# Corpus: one identifiable source field per document
# ------------------------------------------------------------

class Corpus:
    def __init__(self, game):
        self.game = game
        self.docs = []
        self.warnings = []
        self.total_chars = 0

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def add(
        self,
        text,
        title,
        location,
        source_type,
        field,
        kind="text",
        version="unspecified",
        speaker="",
        cached=False,
        html_text=False,
    ):
        text = clean_html(text) if html_text else normalize(text)

        if not text:
            return

        if len(text) > MAX_FIELD_CHARS:
            raise ValueError(
                f"Source field exceeds {MAX_FIELD_CHARS} characters: "
                f"{title} / {field}. Split it into smaller records."
            )

        if len(self.docs) >= MAX_SOURCE_FIELDS:
            raise ValueError("Source-field limit reached.")

        if self.total_chars + len(text) > MAX_CORPUS_CHARS:
            raise ValueError("Corpus character limit reached.")

        source_id = f"S{len(self.docs) + 1:05d}"

        self.docs.append({
            "source_id": source_id,
            "game": self.game,
            "title": str(title),
            "location": str(location),
            "source_type": str(source_type),
            "field": str(field),
            "kind": str(kind).lower(),
            "version": str(version or "unspecified"),
            "speaker": str(speaker or ""),
            "observed_at_utc": utc_now(),
            "response_from_cache": bool(cached),
            "normalized_text_sha256": digest(text),
            "text": text,
        })

        self.total_chars += len(text)


# ------------------------------------------------------------
# League of Legends connector
# ------------------------------------------------------------

def collect_lol(corpus, limit, offset, selected, progress):
    versions, version_cached = get_json(
        "https://ddragon.leagueoflegends.com/api/versions.json"
    )

    if not versions:
        raise ValueError("No Data Dragon versions returned.")

    version = str(versions[0])

    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Unexpected Data Dragon version format.")

    base = (
        "https://ddragon.leagueoflegends.com/"
        f"cdn/{version}/data/en_US"
    )

    corpus.warn(
        f"Data Dragon dataset: {version}. "
        "This is not a verification of your regional client patch."
    )

    if version_cached:
        corpus.warn(
            "The Data Dragon version list came from the local "
            "one-hour API cache."
        )

    needs_text = bool(set(selected) & TEXT_CATEGORIES)
    want_champions = (
        bool(set(selected) & {"Character", "Move", "Term"})
        or needs_text
    )
    want_items = "Item" in selected or needs_text

    if want_champions:
        index_url = f"{base}/champion.json"
        index, _ = get_json(index_url)

        # Sort for stable batches within a given dataset version.
        champion_ids = sorted(index["data"])
        chosen = champion_ids[offset:offset + limit]

        if not chosen:
            corpus.warn("No champions in the selected offset range.")

        for number, champion_id in enumerate(chosen, start=1):
            progress(
                0.05 + 0.35 * number / max(1, len(chosen)),
                desc=f"Loading champion {number}/{len(chosen)}",
            )

            if not re.fullmatch(r"[A-Za-z0-9_]+", champion_id):
                raise ValueError("Unexpected champion identifier.")

            url = f"{base}/champion/{champion_id}.json"
            data, cached = get_json(url)
            champion = data["data"][champion_id]

            root = f"/data/{pointer_token(champion_id)}"
            title = champion["name"]

            def add(value, field, kind="text"):
                corpus.add(
                    value,
                    title,
                    url,
                    "publisher_dataset",
                    root + field,
                    kind=kind,
                    version=version,
                    cached=cached,
                    html_text=True,
                )

            add(champion["name"], "/name", "character")
            add(champion.get("title", ""), "/title", "term")

            if needs_text:
                add(champion.get("lore", ""), "/lore")

            passive = champion.get("passive", {})
            add(passive.get("name", ""), "/passive/name", "move")

            if needs_text:
                add(
                    passive.get("description", ""),
                    "/passive/description",
                )

            for i, spell in enumerate(champion.get("spells", [])):
                add(
                    spell.get("name", ""),
                    f"/spells/{i}/name",
                    "move",
                )

                if needs_text:
                    add(
                        spell.get("description", ""),
                        f"/spells/{i}/description",
                    )

    if want_items:
        url = f"{base}/item.json"
        data, cached = get_json(url)

        item_ids = sorted(data["data"], key=lambda key: int(key))
        chosen = item_ids[offset:offset + limit]

        if not chosen:
            corpus.warn("No items in the selected offset range.")

        for item_id in chosen:
            item = data["data"][item_id]
            root = f"/data/{pointer_token(item_id)}"
            title = item["name"]

            corpus.add(
                item["name"],
                title,
                url,
                "publisher_dataset",
                root + "/name",
                kind="item",
                version=version,
                cached=cached,
                html_text=True,
            )

            if needs_text:
                corpus.add(
                    item.get("description", ""),
                    title,
                    url,
                    "publisher_dataset",
                    root + "/description",
                    version=version,
                    cached=cached,
                    html_text=True,
                )

    if "Place" in selected:
        corpus.warn(
            "This LoL connector has no structured place-name source. "
            "Place results, if any, are NLP candidates only."
        )

    if "Expression" in selected:
        corpus.warn(
            "The LoL connector does not load dialogue. "
            "It will not label lore or ability descriptions as dialogue."
        )

    corpus.warn(
        "The batch size limits champion records and item records "
        "separately. Champion abilities belong to the selected champions."
    )


# ------------------------------------------------------------
# Pokémon connector
# ------------------------------------------------------------

def collect_pokemon(corpus, limit, offset, selected, progress):
    endpoint_map = [
        ("pokemon-species", "Creature", "creature"),
        ("move", "Move", "move"),
        ("location", "Place", "place"),
        ("item", "Item", "item"),
    ]

    needs_text = bool(set(selected) & TEXT_CATEGORIES)

    for endpoint_number, (endpoint, category, kind) in enumerate(
        endpoint_map
    ):
        # Locations generally contribute names, not prose.
        if category not in selected:
            if not needs_text or endpoint == "location":
                continue

        list_url = (
            f"https://pokeapi.co/api/v2/{endpoint}/"
            f"?limit={limit}&offset={offset}"
        )

        listing, _ = get_json(list_url)
        results = listing.get("results", [])

        if not results:
            corpus.warn(f"No {endpoint} records in this range.")

        for i, resource in enumerate(results):
            progress(
                0.05 + 0.45 * (
                    endpoint_number + (i + 1) / max(1, len(results))
                ) / len(endpoint_map),
                desc=f"Loading {endpoint}: {i + 1}/{len(results)}",
            )

            url = resource["url"]
            obj, cached = get_json(url)

            name_entries = [
                (j, entry)
                for j, entry in enumerate(obj.get("names", []))
                if entry.get("language", {}).get("name") == "en"
            ]

            if not name_entries:
                corpus.warn(
                    f"Skipped {endpoint}/{obj.get('id')}: "
                    "no English display name. Internal slugs were not "
                    "substituted for display names."
                )
                continue

            name_index, name_entry = name_entries[0]
            title = name_entry["name"]

            corpus.add(
                title,
                title,
                url,
                "community_dataset",
                f"/names/{name_index}/name",
                kind=kind,
                version="franchise dataset; name not release-filtered",
                cached=cached,
            )

            if not needs_text:
                continue

            # Select one available English flavor-text record.
            # Its actual version label is retained.
            # The first entry is NOT called the latest entry.
            descriptions = obj.get("flavor_text_entries", [])

            for j, entry in enumerate(descriptions):
                if entry.get("language", {}).get("name") != "en":
                    continue

                text_key = (
                    "flavor_text"
                    if "flavor_text" in entry
                    else "text"
                )
                text = entry.get(text_key, "")

                if not text:
                    continue

                version_obj = (
                    entry.get("version")
                    or entry.get("version_group")
                    or {}
                )

                corpus.add(
                    text,
                    title,
                    url,
                    "community_dataset",
                    f"/flavor_text_entries/{j}/{text_key}",
                    version=version_obj.get("name", "unspecified"),
                    cached=cached,
                )
                break

    corpus.warn(
        "PokéAPI is treated as a community dataset. "
        "Species are categorized as Creature, not human Character."
    )
    corpus.warn(
        "The Pokémon batch is franchise-wide, not filtered to one game "
        "release. Description version labels are preserved when provided."
    )

    if "Character" in selected:
        corpus.warn(
            "This connector does not provide a structured trainer/NPC list."
        )

    if "Expression" in selected:
        corpus.warn(
            "This connector does not load dialogue. "
            "Flavor text is not treated as spoken dialogue."
        )


# ------------------------------------------------------------
# Upload connector
# ------------------------------------------------------------

def collect_uploads(corpus, files, txt_kind):
    if not files:
        raise ValueError("Upload at least one TXT, CSV, or JSON file.")

    if len(files) > MAX_FILES:
        raise ValueError(f"Upload at most {MAX_FILES} files.")

    for file_number, file_path in enumerate(files, start=1):
        path = Path(str(file_path))

        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(
                f"{path.name}: exceeds the 1 MB per-file limit."
            )

        raw = path.read_bytes()
        raw_hash = hashlib.sha256(raw).hexdigest()

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{path.name}: save the file as UTF-8 first."
            ) from exc

        # This is a provenance label, never a network fetch address.
        source_location = (
            f"upload:{file_number}:{path.name}#sha256={raw_hash}"
        )

        suffix = path.suffix.lower()

        def add_record(record, field):
            if not isinstance(record, dict):
                raise ValueError(
                    f"{path.name}: every record must be an object."
                )

            value = record.get("text")

            if not isinstance(value, str):
                raise ValueError(
                    f"{path.name}: every record needs a string 'text'."
                )

            kind = str(record.get("category", "text")).strip().lower()

            allowed = set(STRUCTURED_CATEGORIES) | {"text", "dialogue"}

            if kind not in allowed:
                raise ValueError(
                    f"{path.name}: unsupported category '{kind}'."
                )

            corpus.add(
                value,
                record.get("id") or path.name,
                source_location,
                "user_supplied_unverified",
                field,
                kind=kind,
                version=record.get("version", "user-unspecified"),
                speaker=record.get("speaker", ""),
            )

        if suffix == ".txt":
            for line_number, line in enumerate(text.splitlines(), start=1):
                if line.strip():
                    corpus.add(
                        line,
                        path.name,
                        source_location,
                        "user_supplied_unverified",
                        f"line:{line_number}",
                        kind=txt_kind,
                        version="user-unspecified",
                    )

        elif suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text))

            if not reader.fieldnames or "text" not in reader.fieldnames:
                raise ValueError(
                    f"{path.name}: CSV must have a 'text' column."
                )

            for record_number, record in enumerate(reader, start=1):
                add_record(record, f"csv-record:{record_number}/text")

        elif suffix == ".json":
            data = json.loads(text)

            if not isinstance(data, list):
                raise ValueError(
                    f"{path.name}: JSON must be a list of records."
                )

            for record_number, record in enumerate(data):
                add_record(record, f"/{record_number}/text")

        else:
            raise ValueError(f"Unsupported file: {path.name}")

    corpus.warn(
        "Uploaded source authenticity, game identity, and category labels "
        "are user-declared, not independently verified."
    )


# ------------------------------------------------------------
# Extraction
# ------------------------------------------------------------

def extract_occurrences(corpus, selected, progress):
    rows = []
    seen = set()
    selected = set(selected)

    def add(source, category, start, end, method, status):
        if category not in selected:
            return

        term = source["text"][start:end]

        if not term.strip():
            return

        key = (
            source["source_id"],
            category,
            start,
            end,
            method,
        )

        if key in seen:
            return

        if len(rows) >= MAX_OCCURRENCES:
            raise ValueError(
                "Extraction exceeded the occurrence limit. "
                "Use a smaller batch or fewer categories."
            )

        seen.add(key)

        context_start = max(0, start - 90)
        context_end = min(len(source["text"]), end + 90)

        rows.append({
            "game": source["game"],
            "category": category,
            "term": term,
            "method": method,
            "review_status": status,
            "source_id": source["source_id"],
            "source_title": source["title"],
            "source_location": source["location"],
            "source_type": source["source_type"],
            "source_field": source["field"],
            "version": source["version"],
            "speaker": source["speaker"],
            "start": start,
            "end": end,
            "context": source["text"][context_start:context_end],
        })

    prose_sources = []

    for source in corpus.docs:
        structured = STRUCTURED_CATEGORIES.get(source["kind"])

        if structured:
            uploaded = source["source_type"] == "user_supplied_unverified"

            add(
                source,
                structured,
                0,
                len(source["text"]),
                (
                    "user_declared_structured_field"
                    if uploaded
                    else "dataset_structured_field"
                ),
                (
                    "review_user_label"
                    if uploaded
                    else "source_field_not_manually_reviewed"
                ),
            )
        else:
            prose_sources.append(source)

    needs_nlp = bool(
        selected & {
            "Character", "Place", "Term",
            "Phrase", "Clause", "Expression",
        }
    )

    if not needs_nlp or not prose_sources:
        return rows

    nlp = load_nlp()

    for i, (source, doc) in enumerate(
        zip(
            prose_sources,
            nlp.pipe(
                [source["text"] for source in prose_sources],
                batch_size=16,
            ),
        ),
        start=1,
    ):
        progress(
            0.55 + 0.35 * i / len(prose_sources),
            desc=f"Analyzing source field {i}/{len(prose_sources)}",
        )

        # Generic NER results remain candidates.
        for entity in doc.ents:
            if entity.label_ == "PERSON":
                category = "Character"
            elif entity.label_ in {"GPE", "LOC", "FAC"}:
                category = "Place"
            else:
                continue

            add(
                source,
                category,
                entity.start_char,
                entity.end_char,
                f"ner_{entity.label_}",
                "candidate_needs_review",
            )

        for chunk in doc.noun_chunks:
            meaningful = [
                token for token in chunk
                if token.is_alpha and not token.is_stop
            ]

            if not meaningful or len(chunk) > 10:
                continue

            if len(chunk) >= 2:
                add(
                    source,
                    "Phrase",
                    chunk.start_char,
                    chunk.end_char,
                    "noun_chunk",
                    "candidate_needs_review",
                )

            # Term candidates are noun chunks, not asserted terminology.
            add(
                source,
                "Term",
                chunk.start_char,
                chunk.end_char,
                "noun_chunk_term_candidate",
                "candidate_needs_review",
            )

        for token in doc:
            if token.dep_ not in {"advcl", "ccomp", "xcomp", "relcl"}:
                continue

            subtree = sorted(token.subtree, key=lambda item: item.i)
            indices = [item.i for item in subtree]

            # Do not join separated words into invented text.
            if indices != list(range(indices[0], indices[-1] + 1)):
                continue

            if not 3 <= len(subtree) <= 35:
                continue

            add(
                source,
                "Clause",
                subtree[0].idx,
                subtree[-1].idx + len(subtree[-1].text),
                f"dependency_{token.dep_}",
                "candidate_needs_review",
            )

        # Only user-designated dialogue produces expression candidates.
        if source["kind"] == "dialogue":
            for sentence in doc.sents:
                word_count = sum(
                    token.is_alpha for token in sentence
                )

                if 2 <= word_count <= 30:
                    add(
                        source,
                        "Expression",
                        sentence.start_char,
                        sentence.end_char,
                        "short_dialogue_sentence",
                        "candidate_not_verified_idiom",
                    )

    return rows


# ------------------------------------------------------------
# Summary and export
# ------------------------------------------------------------

def build_summary(rows):
    groups = {}

    for row in rows:
        key = (
            row["game"],
            row["category"],
            row["term"].casefold(),
        )

        if key not in groups:
            groups[key] = {
                "game": row["game"],
                "category": row["category"],
                "term": row["term"],
                "count": 0,
                "sources": set(),
                "methods": set(),
                "statuses": set(),
            }

        group = groups[key]
        group["count"] += 1
        group["sources"].add(row["source_id"])
        group["methods"].add(row["method"])
        group["statuses"].add(row["review_status"])

    output = []

    for group in groups.values():
        output.append({
            "game": group["game"],
            "category": group["category"],
            "term": group["term"],
            "extracted_mentions": group["count"],
            "source_fields": len(group["sources"]),
            "methods": "; ".join(sorted(group["methods"])),
            "review_status": "; ".join(sorted(group["statuses"])),
        })

    return sorted(
        output,
        key=lambda row: (
            row["category"],
            -row["extracted_mentions"],
            row["term"].casefold(),
        ),
    )


def export_results(corpus, rows, settings):
    directory = Path(tempfile.mkdtemp(prefix="gametermlab_"))

    summary = build_summary(rows)

    occurrences_frame = pd.DataFrame(
        rows,
        columns=OCCURRENCE_COLUMNS,
    )
    summary_frame = pd.DataFrame(
        summary,
        columns=SUMMARY_COLUMNS,
    )

    safe_occurrences = safe_dataframe(occurrences_frame)
    safe_summary = safe_dataframe(summary_frame)

    safe_summary.to_csv(
        directory / "terms.csv",
        index=False,
        encoding="utf-8-sig",
    )
    safe_occurrences.to_csv(
        directory / "occurrences.csv",
        index=False,
        encoding="utf-8-sig",
    )

    with pd.ExcelWriter(
        directory / "gametermlab.xlsx",
        engine="openpyxl",
    ) as writer:
        safe_summary.to_excel(
            writer, sheet_name="Terms", index=False
        )
        safe_occurrences.to_excel(
            writer, sheet_name="Occurrences", index=False
        )

    for category in CATEGORIES:
        category_frame = safe_summary[
            safe_summary["category"] == category
        ]

        if not category_frame.empty:
            category_frame.to_csv(
                directory / f"{category.lower()}.csv",
                index=False,
                encoding="utf-8-sig",
            )

    manifest = {
        "tool": APP_NAME,
        "created_at_utc": utc_now(),
        "game": corpus.game,
        "settings": settings,
        "source_fields": len(corpus.docs),
        "normalized_corpus_characters": corpus.total_chars,
        "unique_summary_entries": len(summary),
        "extracted_occurrences": len(rows),
        "warnings": corpus.warnings,
        "notes": [
            "This is a bounded batch, not a complete game corpus.",
            "All extracted strings are spans of normalized source fields.",
            "Offsets are zero-based; end offset is exclusive.",
            "HTML is removed from API descriptions.",
            "Whitespace is normalized before extraction.",
            "NLP categories are candidates, not canonical labels.",
            "Extracted mentions are not whole-game frequency estimates.",
            "Different records/versions may contain repeated text.",
            "Public API responses can be cached for up to one hour.",
            "Observed timestamps are app observation times, not proof "
            "of when the upstream data was originally published.",
            "Uploaded-source authenticity is not independently verified.",
            "CSV/XLSX cells are sanitized; JSON preserves extracted text.",
            "Full source texts are not bundled in this export.",
        ],
    }

    source_metadata = [
        {key: value for key, value in doc.items() if key != "text"}
        for doc in corpus.docs
    ]

    json_files = {
        "results.json": {
            "terms": summary,
            "occurrences": rows,
        },
        "sources.json": source_metadata,
        "manifest.json": manifest,
    }

    for filename, data in json_files.items():
        (directory / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    archive = directory / "gametermlab_export.zip"

    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as bundle:
        for path in sorted(directory.iterdir()):
            if path != archive:
                bundle.write(path, arcname=path.name)

    downloads = [
        str(archive),
        str(directory / "gametermlab.xlsx"),
        str(directory / "terms.csv"),
        str(directory / "occurrences.csv"),
        str(directory / "results.json"),
        str(directory / "sources.json"),
        str(directory / "manifest.json"),
    ]

    return summary_frame, downloads


# ------------------------------------------------------------
# Main application callback
# ------------------------------------------------------------

def run_extraction(
    game,
    source_mode,
    custom_game,
    categories,
    limit,
    offset,
    uploads,
    txt_is_dialogue,
    permission_confirmed,
    progress=gr.Progress(),
):
    empty = pd.DataFrame(columns=SUMMARY_COLUMNS)

    if not permission_confirmed:
        return (
            "Please confirm that you are authorized to use the sources.",
            empty,
            [],
        )

    if not categories:
        return "Select at least one category.", empty, []

    categories = [
        category for category in categories
        if category in CATEGORIES
    ]

    limit = max(1, min(int(limit), 30))
    offset = max(0, min(int(offset), 5000))

    upload_mode = (
        source_mode == "Uploaded files"
        or game == GAMES[2]
    )

    if game == GAMES[2]:
        game_name = normalize(custom_game)

        if not game_name:
            return "Enter the name of your game.", empty, []
    else:
        game_name = game.split(" — ")[0]

    corpus = Corpus(game_name)

    progress(0.01, desc="Preparing sources")

    try:
        if upload_mode:
            collect_uploads(
                corpus,
                uploads,
                "dialogue" if txt_is_dialogue else "text",
            )
        elif game == GAMES[0]:
            collect_lol(
                corpus, limit, offset, categories, progress
            )
        elif game == GAMES[1]:
            collect_pokemon(
                corpus, limit, offset, categories, progress
            )
        else:
            raise ValueError("No connector selected.")

    except Exception as exc:
        # Preserve previously collected fields but explicitly label
        # the run as partial. Never substitute another source.
        corpus.warn(
            f"PARTIAL COLLECTION: {type(exc).__name__}: {exc}"
        )

    if not corpus.docs:
        message = "No usable source fields were collected."

        if corpus.warnings:
            message += "\n\n" + "\n".join(corpus.warnings)

        return message, empty, []

    try:
        rows = extract_occurrences(
            corpus, categories, progress
        )

        settings = {
            "selected_game": game,
            "actual_game_label": game_name,
            "source_mode": (
                "Uploaded files" if upload_mode else "Live connector"
            ),
            "categories": categories,
            "batch_size": limit,
            "offset": offset,
            "txt_marked_as_dialogue": bool(txt_is_dialogue),
            "language": "English",
        }

        progress(0.95, desc="Writing exports")

        summary, downloads = export_results(
            corpus, rows, settings
        )

    except Exception as exc:
        return (
            f"Extraction/export failed: {type(exc).__name__}: {exc}",
            empty,
            [],
        )

    category_counts = Counter(
        row["category"] for row in rows
    )

    lines = [
        f"Finished: {game_name}",
        f"Source fields: {len(corpus.docs)}",
        f"Unique category/term entries: {len(summary)}",
        f"Extracted occurrences: {len(rows)}",
        "",
        "Occurrence counts by category:",
    ]

    for category in CATEGORIES:
        lines.append(
            f"- {category}: {category_counts.get(category, 0)}"
        )

    if not rows:
        lines.append(
            "\nNo terms matched the selected categories. "
            "Source metadata and the manifest were still exported."
        )

    lines.extend([
        "",
        "The preview shows at most 500 summary rows.",
        "Downloads contain all extracted rows in this run.",
        "Review NLP candidates before using them as a glossary.",
    ])

    if corpus.warnings:
        lines.append("\nWarnings:")
        lines.extend(f"- {warning}" for warning in corpus.warnings)

    progress(1.0, desc="Finished")

    return (
        "\n".join(lines),
        summary.head(500),
        downloads,
    )


# ------------------------------------------------------------
# User interface
# ------------------------------------------------------------

with gr.Blocks(title="GameTermLab | 遊戲術語擷取工具") as demo:
    gr.Markdown(
        """
# GameTermLab · 遊戲術語擷取工具

**Real source fields → traceable terms → reviewable exports**

Extract names and terminology from built-in game datasets or
your own authorized source files.

**English NLP only. No invented game dialogue.**
"""
    )

    with gr.Row():
        game_input = gr.Dropdown(
            choices=GAMES,
            value=GAMES[0],
            label="Game / 遊戲",
        )

        mode_input = gr.Radio(
            choices=["Live connector", "Uploaded files"],
            value="Live connector",
            label="Source mode / 來源模式",
        )

    custom_game_input = gr.Textbox(
        label="Game name for 'Other game' / 其他遊戲名稱",
        placeholder="Enter your game's actual title",
    )

    category_input = gr.CheckboxGroup(
        choices=CATEGORIES,
        value=[
            "Character",
            "Creature",
            "Move",
            "Place",
            "Item",
            "Term",
            "Phrase",
            "Clause",
            "Expression",
        ],
        label="Extraction categories / 擷取類別",
    )

    with gr.Row():
        limit_input = gr.Slider(
            minimum=1,
            maximum=30,
            step=1,
            value=10,
            label="Live batch size per source group",
        )

        offset_input = gr.Number(
            value=0,
            minimum=0,
            maximum=5000,
            precision=0,
            label="Live source offset: 0, 10, 20...",
        )

    gr.Markdown(
        """
**Batch scope**

- LoL: selected champion batch plus selected item batch;
  abilities come from those champions.
- Pokémon: a separate batch for each requested resource group.
- Uploads: the batch size and offset controls do not apply.
- This is not a complete-game crawl.
"""
    )

    upload_input = gr.File(
        label="Upload UTF-8 TXT, CSV, JSON / 上傳來源檔案",
        file_count="multiple",
        file_types=[".txt", ".csv", ".json"],
        type="filepath",
    )

    dialogue_input = gr.Checkbox(
        value=False,
        label=(
            "Treat TXT lines as dialogue "
            "/ 將 TXT 每行視為對話"
        ),
    )

    with gr.Accordion("Upload format and limits", open=False):
        gr.Markdown(
            """
### TXT
One source line per non-empty line.

### CSV
Required column: `text`

Optional columns:
`id`, `category`, `speaker`, `version`

### JSON
A list of objects using those same field names.

### Supported input categories
`character`, `creature`, `move`, `place`, `item`, `term`,
`text`, `dialogue`

Use `text` for unclassified prose.
Use `dialogue` only when the record is actually dialogue.

A structured category means the entire `text` field is a name/term.
Do not label a whole paragraph as `character` or `move`.

### Limits
- 10 files per run.
- 1 MB per file.
- 6,000 normalized characters per field.
- 150,000 normalized characters across the corpus.
- 3,000 source fields.
- 30,000 extracted occurrences.

Split larger inputs into batches.
"""
        )

    permission_input = gr.Checkbox(
        value=False,
        label=(
            "I am authorized to use these sources and will respect "
            "their applicable usage conditions."
        ),
    )

    start_button = gr.Button(
        "Start extraction / 開始擷取",
        variant="primary",
    )

    status_output = gr.Textbox(
        label="Status and warnings / 狀態與提醒",
        lines=12,
        interactive=False,
    )

    preview_output = gr.Dataframe(
        headers=SUMMARY_COLUMNS,
        interactive=False,
        label="Term summary preview / 術語預覽",
    )

    download_output = gr.File(
        label="Download exports / 下載匯出檔案",
        file_count="multiple",
    )

    gr.Markdown(
        """
### Interpretation and privacy

- Dataset names are source-backed fields, not manually reviewed results.
- NLP results remain candidates.
- Expressions are short dialogue sentences, not verified idioms.
- Source excerpts and provenance accompany extracted occurrences.
- Uploaded text is processed on the machine hosting this application.
- Only built-in public API addresses are requested.
- This prototype is intended for local, single-user use.
- Export files remain in the host's temporary directory until removed.
- Do not upload confidential or unauthorized material.

**GitHub project:** `digimarketingai/gametermlab`
"""
    )

    start_button.click(
        fn=run_extraction,
        inputs=[
            game_input,
            mode_input,
            custom_game_input,
            category_input,
            limit_input,
            offset_input,
            upload_input,
            dialogue_input,
            permission_input,
        ],
        outputs=[
            status_output,
            preview_output,
            download_output,
        ],
        concurrency_limit=1,
        trigger_mode="once",
        api_name=False,
    )

demo.queue(
    max_size=4,
    default_concurrency_limit=1,
)


def launch(share=False):
    username = os.environ.get("GAMETERMLAB_USER", "")
    password = os.environ.get("GAMETERMLAB_PASSWORD", "")

    if share and not (username and password):
        raise RuntimeError(
            "Public sharing requires GAMETERMLAB_USER and "
            "GAMETERMLAB_PASSWORD environment variables."
        )

    auth = (username, password) if username and password else None

    return demo.launch(
        share=share,
        auth=auth,
        max_file_size="1mb",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--share",
        action="store_true",
        help="Request an authenticated public Gradio share link.",
    )
    args = parser.parse_args()
    launch(share=args.share)
