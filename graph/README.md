# Лёгкий граф

`nodes.ndjson` содержит узлы, `edges.ndjson` — направленные связи. Одна строка равна одному JSON-объекту; это упрощает diff, поиск и потоковую обработку.

Типы узлов: process, catalog, document, register, report, setting, role, mechanism, article, subsystem, form, command, attribute. Для XML метаданных дополнительно использовать связи has_form, has_command, has_attribute, has_tabular_section, included_in, grants_access, implemented_by и sourced_from.

Идентификаторы стабильны. Граф служит индексом, а не заменяет статьи: подробное утверждение должно вести в `source_refs`.
