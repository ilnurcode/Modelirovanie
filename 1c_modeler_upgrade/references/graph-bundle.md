# Состав графов

## 1. Граф источников

`1c_erp_2_5_source_graph.json` строится из ИТС и интерфейса. Используйте для поиска статей, маршрутов и функциональных опций. Это не граф прикладной модели ERP.

```sh
python3 scripts/build_graph.py --raw "<ИТС ERP>" --interface "<Интерфейс ERP>" --output 1c_erp_2_5_source_graph.json
python3 scripts/validate_graph.py --graph 1c_erp_2_5_source_graph.json
```

## 2. Граф объектов ERP

`graphs/1c_erp_2_5_object_graph.json` содержит подтверждённые XML точного релиза
объекты, реквизиты, табличные части, измерения, ресурсы, формы и команды. Он
выделяется из XML semantic graph, поэтому импортированный интерфейсный набор не
является первичным доказательством.

```powershell
py -3 1c_modeler_upgrade/scripts/build_exact_object_graph.py `
  --semantic-graph 1c_modeler_upgrade/graphs/1c_erp_2_5_semantic_graph.json `
  --output 1c_modeler_upgrade/graphs/1c_erp_2_5_object_graph.json
```

## 3. Граф маршрутов

`graphs/1c_erp_2_5_route_graph.json` строится из локальной XML-выгрузки точного
релиза. Источниками служат `Subsystem.xml`, `CommandInterface.xml` и индекс
представлений объектов. Импортированный граф хранится только в
`legacy/imported/` и не участвует в генерации.

Перестроить активный граф для ERP 2.5.27.49:

```powershell
py -3 scripts/build_1c_route_graph.py `
  --repo . `
  --config local/configurations/erp-2.5.27.49 `
  --output 1c_modeler_upgrade/graphs/1c_erp_2_5_route_graph.json
py -3 1c_modeler_upgrade/scripts/build_search_index.py --root 1c_modeler_upgrade
```

Путь получает `verified_metadata`, когда его исходный XML доступен и релиз графа
совпадает с проектом. Состав и подписи полей формы, бизнес-связи и действия
`создать на основании` этот граф не подтверждает.

## 4. Семантический граф

`graphs/1c_erp_2_5_semantic_graph.json` строится из локальной XML-выгрузки точного
релиза. В него входят `BasedOn`, объявленные наборы записей регистров, состав
объектов и ссылочные типы реквизитов. Каждое ребро содержит `source_ref`,
`source_xpath` и `verification_status: verified_metadata`.

Перестроить граф для ERP 2.5.27.49:

```powershell
py -3 1c_modeler_upgrade/scripts/build_semantic_graph.py `
  --repo . `
  --config local/configurations/erp-2.5.27.49 `
  --metadata-index metadata/index/objects.ndjson `
  --configuration-manifest metadata/index/configuration.json `
  --output 1c_modeler_upgrade/graphs/1c_erp_2_5_semantic_graph.json
py -3 1c_modeler_upgrade/scripts/build_search_index.py --root 1c_modeler_upgrade
```

Статус `ГОТОВ` означает полноту только в заявленном структурном XML-контуре.
Отсутствующую бизнес-связь предполагать нельзя. `declares_register_records` не
гарантирует фактическое движение при любых условиях проведения.

## 5. Граф процесса заказчика

`graphs/process_graph_template.json` заполняется отдельно для каждого процесса. Содержит шаги, роли, решения, входы и выходы. Не смешивайте его с типовой моделью ERP.

## 6. Поисковый индекс

`graphs/search-index.ndjson.gz` содержит компактное представление узлов всех четырёх
графов. Приложение ищет по нему релевантные узлы и передаёт агенту только найденный
контекст. Перестроить индекс:

```powershell
py -3 scripts/build_search_index.py
```

Запись object graph получает `verified_metadata` только когда её `source_xml` найден в
локальной выгрузке точного релиза проекта. Остальные совпадения остаются `inferred`.

## Статусы

- `ГОТОВ`: граф можно использовать.
- `НЕ_ЗАПОЛНЕН`: граф-шаблон. Запрещено строить по нему инструкции.
- `НЕПОЛНЫЙ`: допускается только анализ и вопросы.

Перед формированием плана source, object, route и semantic graphs должны иметь
статус `ГОТОВ`. Для каждой связи между объектами требуется ребро semantic graph
или первичный источник. `inferred` и неразрешённые проверки блокируют итоговый
апрув.
