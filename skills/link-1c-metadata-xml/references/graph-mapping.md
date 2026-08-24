# Правила отображения XML в граф

| XML-сущность | Тип узла | Пример идентификатора |
|---|---|---|
| Document | document | md-document-supplier-order |
| Catalog | catalog | md-catalog-suppliers |
| InformationRegister | register | md-register-purchase-prices |
| AccumulationRegister | register | md-register-goods-to-receive |
| Report | report | md-report-order-state |
| Role | role | md-role-procurement-manager |
| Subsystem | subsystem | md-subsystem-procurement |
| Form | form | md-form-supplier-order-document |
| Command | command | md-command-create-receipt |
| Attribute | attribute | md-attribute-supplier-order-status |

Использовать связи has_form, has_command, has_attribute, has_tabular_section, included_in, grants_access, implemented_by и sourced_from. В узле сохранять internal_name, synonym, uuid, configuration_version, source_path и verification_status.

Пользовательский путь считать подтверждённым только при наличии цепочки subsystem → command → form/object и подходящей роли. Синоним объекта сам по себе не доказывает расположение команды.
