# Контракты проектных артефактов

## Единые поля v4

Во всех артефактах использовать `schema_version: 4`, `project_id` и `revision`. Канонический ID требования — поле `id`; при чтении старых ревизий допустим алиас `requirement_id`.

- `requirement-map.requirements[]`: `id`, `source_text`, `statement`, `cluster`, `source`, `coverage_status`, `decision_ids`, `evidence_ids`, `solution_element_ids`, `acceptance_test_ids`.
- `evidence-map.evidence[]`: `id`, `source_type`, `product`, `release`, `source_ref`, `object_ref`, `field_ref`, `route_ref`, `edge_ref`, `excerpt`, `status`.
- `decision-register.decisions[]`: `id`, `question_id`, `exact_user_answer`, `normalized_value`, `revision`, `affected_requirement_ids`.
- `gap-register.gaps[]`: `id`, `requirement_ids`, `description`, `criticality`, `reason`, `prototype_method`, `closure_criterion`, `status`.
- `acceptance-tests.tests[]`: `id`, `preconditions`, `actions`, `expected_result`, `requirement_ids`.
- `traceability.links[]`: `requirement_id`, `decision_ids`, `evidence_ids`, `solution_element_ids`, `instruction_step_ids`, `acceptance_test_ids`.

Статус evidence — только `verified_source|verified_metadata|user_decision|candidate|unresolved`; покрытие — `covered|partial|gap`; проверка шага — `verified|verified_metadata|needs_review|rejected`.

Каждый вызов `save_artifact` дополнительно передаёт внешний объект `provenance`, возвращённый `start_agent_run`:

```json
{
  "provider": "фактический provider",
  "model": "фактический model id",
  "parameters": {},
  "skill": "erp-requirements-modeling",
  "skill_version": "sha256:...",
  "run_id": "run-...",
  "policy_id": "erp-agent-model-policy-v1"
}
```

MCP сверяет эти поля с журналом и текущей content-addressed версией skill. Provenance хранится в карточке каждой ревизии артефакта.

## requirement-map

```json
{
  "requirements": [{
    "requirement_id": "REQ-001",
    "source_text": "исходный фрагмент",
    "statement": "атомарное требование",
    "goal": "бизнес-цель",
    "actors": [],
    "trigger": "",
    "inputs": [],
    "outputs": [],
    "variants": [],
    "exceptions": [],
    "acceptance": [],
    "decision_ids": [],
    "confirmed_node_ids": [],
    "candidate_node_ids": [],
    "open_questions": []
  }]
}
```

## solution-model

```json
{
  "processes": [{
    "id": "PROC-001",
    "title": "",
    "preconditions": [],
    "steps": [{
      "id": "STEP-001",
      "actor": "",
      "action": "",
      "erp_node_ids": [],
      "condition": "",
      "control": ""
    }],
    "branches": [],
    "postconditions": []
  }]
}
```

## traceability

```json
{
  "rows": [{
    "requirement_id": "REQ-001",
    "decision_ids": [],
    "erp_node_ids": [],
    "relations": [],
    "solution_elements": [],
    "acceptance": [],
    "status": "covered|partial|gap",
    "note": ""
  }]
}
```

## quality-gate

```json
{
  "revision": 1,
  "checks": {
    "all_requirements_traced": false,
    "all_exact_erp_names_have_l3_evidence": false,
    "business_behavior_has_l4_or_explicit_decision": false,
    "branches_and_exceptions_modeled": false,
    "acceptance_criteria_present": false,
    "blocking_questions": 0
  },
  "gaps": [],
  "ready_for_approval": false
}
```

`ready_for_approval` допускается только при отсутствии скрытых блокирующих вопросов. Неполное покрытие не маскировать усреднённой оценкой.
