from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import ConfigurationInfo, Project, ProjectMode
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.modeler import ModelerReviewService, _tokens


class ModelerReviewTest(unittest.TestCase):
    def test_large_tz_uses_bounded_modeler_terms(self):
        query = " ".join(f"термин{index}" for index in range(500)) + " склад " * 20
        tokens = _tokens(query)
        self.assertLessEqual(len(tokens), 64)
        self.assertIn("склад", tokens)

    def test_context_reads_compact_index_from_each_graph(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph_dir = root / "1c_modeler_upgrade" / "graphs"
            graph_dir.mkdir(parents=True)
            index = graph_dir / "search-index.ndjson.gz"
            with gzip.open(index, "wt", encoding="utf-8") as stream:
                for graph in ("source", "object", "route", "semantic"):
                    stream.write(
                        json.dumps(
                            {
                                "graph": graph,
                                "configuration": "1С:ERP 2.5",
                                "release": "",
                                "id": f"{graph}.Закупка",
                                "label": "Закупка",
                                "type": "Test",
                                "properties": {},
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            project = Project(
                "test",
                "Test",
                ProjectMode.FULL,
                configuration=ConfigurationInfo("1С:ERP", "", "2.5.27.49"),
            )

            context = ModelerReviewService(RepositoryPaths(root)).context(
                project, "процесс закупки"
            )

            for graph in ("source", "object", "route", "semantic"):
                self.assertIn(f'"{graph}"', context)
                self.assertIn(f'"{graph}.Закупка"', context)

    def test_unconfirmed_release_never_verifies_matching_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph_dir = root / "1c_modeler_upgrade" / "graphs"
            graph_dir.mkdir(parents=True)
            (graph_dir / "graph_manifest.json").write_text(
                json.dumps({"configuration": "1С:ERP 2.5", "graphs": []}),
                encoding="utf-8",
            )
            (graph_dir / "1c_erp_2_5_route_graph.json").write_text(
                json.dumps(
                    {
                        "configuration": "1С:ERP 2.5",
                        "status": "ГОТОВ",
                        "nodes": {
                            "r1": {
                                "id": "r1",
                                "label": "Заказ поставщику",
                                "type": "InterfaceRoute",
                                "properties": {
                                    "path": "Закупки → Создать → Заказ поставщику",
                                    "source_path": "C:/missing/interface.md",
                                },
                            }
                        },
                        "edges": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (graph_dir / "1c_erp_2_5_semantic_graph.json").write_text(
                json.dumps({"status": "НЕПОЛНЫЙ", "nodes": {}, "edges": []}),
                encoding="utf-8",
            )
            project = Project(
                "test",
                "Test",
                ProjectMode.FULL,
                configuration=ConfigurationInfo(
                    "1С:ERP Управление предприятием 2", "2.5", "2.5.27.49"
                ),
            )
            result = {
                "steps": [
                    {
                        "id": "P01",
                        "ui_path": "Закупки → Создать → Заказ поставщику",
                    }
                ]
            }

            report = ModelerReviewService(RepositoryPaths(root)).review(project, result)

            self.assertEqual("product_only", report["compatibility"])
            self.assertEqual("inferred", report["path_checks"][0]["status"])
            self.assertEqual("review_required", report["verdict"])

    def test_short_writer_path_is_expanded_to_full_erp_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph_dir = root / "1c_modeler_upgrade" / "graphs"
            graph_dir.mkdir(parents=True)
            (graph_dir / "graph_manifest.json").write_text(
                json.dumps({"configuration": "1С:ERP 2.5", "graphs": []}),
                encoding="utf-8",
            )
            nodes = {
                "full": {
                    "id": "Route.full",
                    "label": "Заказы клиентов",
                    "type": "InterfaceRoute",
                    "properties": {
                        "path": "Продажи → Оптовые продажи → Заказы клиентов",
                        "technical_name": "Document.ЗаказКлиента.StandardCommand.OpenList",
                        "source_path": "local/configurations/erp-2.5.27.49/full.xml",
                    },
                },
                "basic": {
                    "id": "Route.basic",
                    "label": "Заказы клиентов",
                    "type": "InterfaceRoute",
                    "properties": {
                        "path": "Продажи → Ведение заказов клиентов → Заказы клиентов",
                        "technical_name": "Document.ЗаказКлиента.StandardCommand.OpenList",
                        "source_path": "local/configurations/erp-2.5.27.49/ПродажиБазовая/basic.xml",
                    },
                },
            }
            (graph_dir / "1c_erp_2_5_route_graph.json").write_text(
                json.dumps(
                    {
                        "configuration": "1С:ERP 2.5",
                        "release": "2.5.27.49",
                        "status": "ГОТОВ",
                        "nodes": nodes,
                        "edges": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            project = Project(
                "test",
                "Test",
                ProjectMode.FULL,
                configuration=ConfigurationInfo(
                    "1С:ERP Управление предприятием 2", "2.5", "2.5.27.49"
                ),
            )
            result = {
                "sources": [],
                "document_flow": [],
                "steps": [
                    {
                        "id": "P01",
                        "form": "Document.ЗаказКлиента",
                        "ui_path": "Продажи → Заказы клиентов",
                        "evidence_refs": [],
                    }
                ],
            }
            service = ModelerReviewService(RepositoryPaths(root))

            repairs = service.normalize_ui_paths(project, result)
            report = service.review(project, result)

            self.assertEqual(1, len(repairs))
            self.assertEqual(
                "Продажи → Оптовые продажи → Заказы клиентов",
                result["steps"][0]["ui_path"],
            )
            self.assertEqual(["modeler-route:Route.full"], result["steps"][0]["evidence_refs"])
            self.assertEqual("passed", report["verdict"])
            self.assertEqual("verified_metadata", report["path_checks"][0]["status"])

    def test_exact_document_identity_beats_similarly_named_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph_dir = root / "1c_modeler_upgrade" / "graphs"
            graph_dir.mkdir(parents=True)
            (graph_dir / "graph_manifest.json").write_text(
                json.dumps({"configuration": "1С:ERP 2.5"}), encoding="utf-8"
            )
            (graph_dir / "1c_erp_2_5_route_graph.json").write_text(
                json.dumps(
                    {
                        "configuration": "1С:ERP 2.5",
                        "release": "2.5.27.49",
                        "nodes": {
                            "document": {
                                "id": "Route.document",
                                "label": "Документы внутреннего потребления товаров",
                                "properties": {
                                    "path": "Склад и доставка → Внутреннее товародвижение → Документы внутреннего потребления товаров",
                                    "technical_name": "Document.ВнутреннееПотребление.StandardCommand.OpenList",
                                    "source_path": "local/document.xml",
                                },
                            },
                            "report": {
                                "id": "Route.report",
                                "label": "Результаты согласования заказа на внутреннее потребление",
                                "properties": {
                                    "path": "Склад и доставка → Результаты согласования заказа на внутреннее потребление",
                                    "technical_name": "Report.РезультатыСогласованияЗаказаНаВнутреннееПотребление",
                                    "source_path": "local/report.xml",
                                },
                            },
                            "return": {
                                "id": "Route.return",
                                "label": "Возвраты товаров от клиентов",
                                "properties": {
                                    "path": "Продажи → Оптовые продажи → Возвраты товаров от клиентов",
                                    "technical_name": "Document.ВозвратТоваровОтКлиента.StandardCommand.OpenList",
                                    "source_path": "local/return.xml",
                                },
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            project = Project(
                "test",
                "Test",
                ProjectMode.FULL,
                configuration=ConfigurationInfo("1С:ERP", "2.5", "2.5.27.49"),
            )
            result = {
                "sources": [],
                "document_flow": [],
                "steps": [
                    {
                        "id": "P10",
                        "form": "Документ «Возврат товаров от клиента» → Документ «Внутреннее потребление»",
                        "ui_path": "Продажи → Возвраты товаров от клиентов; затем Склад и доставка → Внутреннее потребление",
                        "evidence_refs": [],
                    }
                ],
            }

            ModelerReviewService(RepositoryPaths(root)).normalize_ui_paths(project, result)

            self.assertEqual(
                "Продажи → Оптовые продажи → Возвраты товаров от клиентов; Склад и доставка → Внутреннее товародвижение → Документы внутреннего потребления товаров",
                result["steps"][0]["ui_path"],
            )

    def test_weak_generic_overlap_does_not_replace_ui_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph_dir = root / "1c_modeler_upgrade" / "graphs"
            graph_dir.mkdir(parents=True)
            (graph_dir / "1c_erp_2_5_route_graph.json").write_text(
                json.dumps(
                    {
                        "configuration": "1С:ERP 2.5",
                        "release": "2.5.27.49",
                        "nodes": {
                            "wrong": {
                                "id": "Route.wrong",
                                "label": "Причины отмены производства",
                                "properties": {
                                    "path": "Производство → Настройки и справочники → Причины отмены производства",
                                    "technical_name": "Catalog.ПричиныОтменыПроизводства.StandardCommand.OpenList",
                                    "source_path": "local/wrong.xml",
                                },
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            project = Project(
                "test",
                "Test",
                ProjectMode.FULL,
                configuration=ConfigurationInfo("1С:ERP", "2.5", "2.5.27.49"),
            )
            result = {
                "sources": [],
                "document_flow": [],
                "steps": [
                    {
                        "id": "P02",
                        "form": "Справочник.ПроизводственныеПодразделения",
                        "ui_path": "Производство → Настройки и справочники → Производственные подразделения",
                        "evidence_refs": [],
                    }
                ],
            }

            repairs = ModelerReviewService(RepositoryPaths(root)).normalize_ui_paths(
                project, result
            )

            self.assertEqual([], repairs)
            self.assertEqual([], result["steps"][0]["evidence_refs"])


if __name__ == "__main__":
    unittest.main()
