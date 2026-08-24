from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import ConfigurationInfo
from consultant_cli.infrastructure.store import RepositoryPaths


@dataclass(slots=True)
class SourceCandidate:
    ref: str
    title: str
    score: int
    excerpt: str
    kind: str = "local"


@dataclass(slots=True)
class SourceRoute:
    requested_product: str
    requested_release: str
    local_product: str = ""
    local_release: str = ""
    compatibility: str = "not_checked"
    use_xml: bool = False
    use_local_knowledge: bool = True
    external_docs_required: bool = False
    web_search_required: bool = False
    warnings: list[str] = field(default_factory=list)
    candidates: list[SourceCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        return result


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", value.casefold()).strip()


def _is_erp(product: str) -> bool:
    normalized = _normalized(product)
    return "erp" in normalized or "управление предприятием" in normalized


class SourceRouter:
    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        self.metadata_manifest = paths.root / "metadata" / "index" / "configuration.json"

    def route(self, configuration: ConfigurationInfo, query: str = "") -> SourceRoute:
        manifest = self._manifest()
        local_product = str(manifest.get("synonym", ""))
        local_release = str(manifest.get("version", ""))
        requested = configuration.product.strip()
        release = configuration.release.strip()
        route = SourceRoute(
            requested_product=requested or "not_configured",
            requested_release=release,
            local_product=local_product,
            local_release=local_release,
        )

        if configuration.is_unspecified:
            route.compatibility = "not_configured"
            route.use_xml = False
            route.external_docs_required = True
            route.web_search_required = True
            route.warnings.append(
                "Конфигурация не указана: точные пути и команды нельзя считать проверенными."
            )
        elif _normalized(requested) == _normalized(local_product) and release == local_release:
            route.compatibility = "exact"
            route.use_xml = True
        elif _is_erp(requested):
            route.compatibility = "product_only"
            route.use_xml = False
            route.external_docs_required = True
            route.web_search_required = True
            route.warnings.append(
                f"Локальный XML относится к {local_product} {local_release}; "
                f"для {requested} {release or 'без релиза'} точные UI-шаги по нему не подтверждаются."
            )
        else:
            route.compatibility = "different_product"
            route.use_xml = False
            route.external_docs_required = True
            route.web_search_required = True
            route.warnings.append(
                "Выбрана не ERP-конфигурация: ERP XML и ERP-срезы отключены."
            )

        route.candidates = self.search_local(query, include_metadata_slices=route.use_xml)
        return route

    def _manifest(self) -> dict[str, Any]:
        if not self.metadata_manifest.exists():
            return {}
        try:
            return json.loads(self.metadata_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def search_local(
        self, query: str, limit: int = 12, include_metadata_slices: bool = True
    ) -> list[SourceCandidate]:
        tokens = {
            token
            for token in re.findall(r"[0-9a-zа-яё]+", query.casefold())
            if len(token) >= 4
        }
        roots = [
            self.paths.root / "knowledge" / "articles",
            self.paths.root / "processes",
            self.paths.root / "examples" / "requests",
        ]
        if include_metadata_slices:
            roots.append(self.paths.root / "metadata" / "slices")
        candidates: list[SourceCandidate] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                text = path.read_text(encoding="utf-8", errors="replace")
                folded = text.casefold()
                score = sum(min(folded.count(token), 8) for token in tokens)
                if not tokens:
                    score = 1
                if score <= 0:
                    continue
                title = next(
                    (line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")),
                    path.stem,
                )
                excerpt = self._excerpt(text, tokens)
                candidates.append(
                    SourceCandidate(
                        ref=path.relative_to(self.paths.root).as_posix(),
                        title=title,
                        score=score,
                        excerpt=excerpt,
                    )
                )
        return sorted(candidates, key=lambda item: (-item.score, item.ref))[:limit]

    @staticmethod
    def _excerpt(text: str, tokens: set[str], limit: int = 900) -> str:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        for paragraph in paragraphs:
            folded = paragraph.casefold()
            if tokens and any(token in folded for token in tokens):
                return paragraph[:limit]
        return (paragraphs[0] if paragraphs else "")[:limit]

    @staticmethod
    def context(route: SourceRoute, max_chars: int = 12000) -> str:
        header = {
            "requested_product": route.requested_product,
            "requested_release": route.requested_release,
            "compatibility": route.compatibility,
            "use_xml": route.use_xml,
            "external_docs_required": route.external_docs_required,
            "web_search_required": route.web_search_required,
            "warnings": route.warnings,
        }
        blocks = ["Маршрут источников:\n" + json.dumps(header, ensure_ascii=False, indent=2)]
        for item in route.candidates:
            blocks.append(f"SOURCE {item.ref}\nTITLE {item.title}\n{item.excerpt}")
        return "\n\n".join(blocks)[:max_chars]
