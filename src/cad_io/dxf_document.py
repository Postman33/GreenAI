"""DXF loading with disk-backed tag storage for large ASCII drawings.

ezdxf normally retains every raw tag before constructing any CAD entities.
For large drawings, defer BLOCKS and ENTITIES tags to a temporary file. The
same ezdxf loader then resolves handles, dictionaries, layouts and blocks.
This reduces the temporary tag allocation; the resulting Drawing still lives
in memory. No source records are filtered out or rewritten during loading.
"""

from __future__ import annotations

import os
import pickle
import tempfile
from array import array
from pathlib import Path
from typing import Any, BinaryIO, Iterable

import ezdxf
from ezdxf.document import Drawing
from ezdxf.filemanagement import dxf_file_info
from ezdxf.lldxf import loader
from ezdxf.lldxf.tagger import ascii_tags_loader, tag_compiler
from ezdxf.lldxf.tags import group_tags
from ezdxf.lldxf.validator import is_binary_dxf_file


SPOOL_THRESHOLD_BYTES = 64 * 1024 * 1024


class _DeferredTags(list):
    """Mutable section consumed by ezdxf.load_and_bind_dxf_content.

That loader replaces each raw tag record with its loaded entity in place.
Afterwards normal list operations (including removing the section header)
operate exclusively on entities, so the temporary file is no longer needed.
The pickle stream contains only tags produced here, never an external pickle.
"""

    def __init__(self, stream: BinaryIO):
        super().__init__()
        self.stream = stream
        self.offsets = array("Q")

    def append_tags(self, tags: Any) -> None:
        self.offsets.append(self.stream.tell())
        pickle.dump(tags, self.stream, protocol=pickle.HIGHEST_PROTOCOL)
        super().append(None)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        value = super().__getitem__(index)
        if value is None:
            self.stream.seek(self.offsets[index])
            return pickle.load(self.stream)
        return value

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]


def read_dxf_document(
    path: Path, *, spool_threshold_bytes: int = SPOOL_THRESHOLD_BYTES,
    temporary_directory: Path | None = None,
) -> Drawing:
    path = Path(path)
    if path.stat().st_size < spool_threshold_bytes or is_binary_dxf_file(path):
        return ezdxf.readfile(path)

    info = dxf_file_info(path)
    print("Loading large DXF with temporary tag storage", flush=True)
    with tempfile.TemporaryFile(dir=temporary_directory) as spool, path.open(
        encoding=info.encoding, errors="surrogateescape",
    ) as source:
        deferred: dict[str, _DeferredTags] = {}

        def defer_entities(tags: Iterable[Any]) -> Iterable[Any]:
            active: _DeferredTags | None = None
            for record in group_tags(tags):
                if record[0] == (0, "SECTION"):
                    active = None
                    if len(record) > 1 and record[1].code == 2:
                        name = record[1].value
                        if name in {"BLOCKS", "ENTITIES"}:
                            active = _DeferredTags(spool)
                            deferred[name] = active
                            active.append_tags(record)
                    # Keep structural markers in the standard validator.
                    yield from record
                elif record[0] in ((0, "ENDSEC"), (0, "EOF")):
                    active = None
                    yield from record
                elif active is not None:
                    active.append_tags(record)
                else:
                    yield from record

        sections = loader.load_dxf_structure(
            defer_entities(tag_compiler(ascii_tags_loader(source)))
        )
        sections.update(deferred)
        sections.pop("THUMBNAILIMAGE", None)  # Same as Drawing._load().
        document = Drawing()
        # Adapter to ezdxf's loading stages: do not bypass entity binding,
        # post-load hooks, block linking or the normal document audit.
        document._load_section_dict(sections)
    document.filename = str(path)
    return document


def save_dxf_atomic(document: Drawing, path: Path, *, allow_version_fallback: bool) -> Path:
    """Publish a complete drawing; a failed save leaves existing files intact."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.stem}-", suffix=".dxf.tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
    try:
        document.saveas(temporary)
        candidates = [path]
        if allow_version_fallback:
            candidates.extend(path.with_name(f"{path.stem}_v{i}{path.suffix}") for i in range(2, 100))
        last_error: PermissionError | None = None
        for candidate in candidates:
            try:
                os.replace(temporary, candidate)
            except PermissionError as error:
                last_error = error
                continue
            document.filename = str(candidate)
            return candidate
        assert last_error is not None
        raise last_error
    finally:
        temporary.unlink(missing_ok=True)
