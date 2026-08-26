import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type JsonObject = Record<string, any>;

function parseJson(text: string): JsonObject {
  const start = text.indexOf("{");
  const arrayStart = text.indexOf("[");
  const position = start < 0 ? arrayStart : arrayStart < 0 ? start : Math.min(start, arrayStart);
  if (position < 0) throw new Error(text.trim() || "Python runtime не вернул JSON");
  return JSON.parse(text.slice(position));
}

function friendlyError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes("Пакет вопросов ещё не сформирован")) {
    return "Сначала выберите «Сформировать пакет вопросов · translator/low».";
  }
  if (message.includes("Аналитическая модель не найдена")) {
    return "Для старого проекта нужна бесплатная инициализация. Выберите «Предварительная проверка без LLM».";
  }
  return message;
}

function runAction(project: JsonObject): string | undefined {
  const requirementsVersion = Number(project.requirements_version || 0);
  const designVersion = Number(project.design_version || 0);
  const status = String(project.status || "");
  if (requirementsVersion === 0) return "Сформировать пакет вопросов · translator/low";
  if (status === "requirements_approved") return "Сформировать проект решения · planner/medium";
  if (
    designVersion > 0 &&
    ["design_approved", "needs_revision", "draft", "error"].includes(status)
  ) return "Сформировать итоговый ответ · writer/high";
  return undefined;
}

function statusText(project: JsonObject): string {
  const next = runAction(project) || {
    requirements_pending: "Ответить на вопросы и явно утвердить требования",
    design_pending: "Проверить цепочку документов и явно утвердить проект решения",
    feedback_pending: "Проверить Markdown и ответить «Всё устраивает» либо отправить на доработку",
    successful: "Проект подтверждён; можно задавать дополнительные вопросы",
  }[String(project.status || "")] || "Проверьте состояние проекта";
  return [
    `Проект: ${project.title || project.project_id}`,
    `Статус: ${project.status}`,
    `Ревизия: ${project.revision}`,
    `Следующий шаг: ${next}`,
  ].join("\n");
}

export default function (pi: ExtensionAPI) {
  async function call(cwd: string, args: string[], timeout = 1_200_000): Promise<any> {
    const repo = process.env.CONSULTANT_REPO || cwd;
    const executable = process.env.CONSULTANT_EXECUTABLE;
    const command = executable || "powershell.exe";
    const commandArgs = executable
      ? ["--repo", repo, "--json", ...args]
      : ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(repo, "consultant.ps1"), "--repo", repo, "--json", ...args];
    const result = await pi.exec(
      command,
      commandArgs,
      { cwd: repo, timeout }
    );
    if (result.code !== 0) {
      const raw = result.stdout.trim() || result.stderr.trim();
      try {
        const payload = parseJson(raw);
        throw new Error(String(payload.error || raw));
      } catch (error) {
        if (error instanceof SyntaxError) throw new Error(raw || "Python runtime завершился с ошибкой");
        throw error;
      }
    }
    return parseJson(result.stdout);
  }

  async function chooseProject(ctx: any): Promise<string | undefined> {
    const projects = await call(ctx.cwd, ["list"], 60_000) as JsonObject[];
    const labels = projects.map((item) => `${item.project_id} — ${item.title} — ${item.status}`);
    const selected = await ctx.ui.select("ERP-проект", [...labels, "＋ Новый проект", "Отмена"]);
    if (!selected || selected === "Отмена") return undefined;
    if (selected === "＋ Новый проект") {
      const title = await ctx.ui.input("Название проекта", "Например: Производство и доставка");
      if (!title?.trim()) return undefined;
      const sourceMode = await ctx.ui.select("Источник ТЗ", [
        "Загрузить Markdown-файл (.md)", "Вставить текст вручную"
      ]);
      if (!sourceMode) return undefined;
      const sourceArgs: string[] = [];
      if (sourceMode === "Загрузить Markdown-файл (.md)") {
        const file = await ctx.ui.input("Полный путь к Markdown-файлу", "C:\\Users\\...\\Desktop\\tz.md");
        if (!file?.trim()) return undefined;
        sourceArgs.push("--file", file.trim().replace(/^['\"]|['\"]$/g, ""));
      } else {
        const prompt = await ctx.ui.input("Описание процесса / ТЗ", "Можно вставить подробный текст");
        if (!prompt?.trim()) return undefined;
        sourceArgs.push("--prompt", prompt.trim());
      }
      const release = await ctx.ui.input("Точный релиз ERP", "Например: 2.5.27.49");
      if (!release?.trim()) return undefined;
      const created = await call(ctx.cwd, [
        "new", title.trim(), ...sourceArgs, "--product", "1С:ERP Управление предприятием 2",
        "--release", release.trim(), "--deliverable", "hybrid"
      ]);
      return String(created.project_id);
    }
    return selected.split(" — ", 1)[0];
  }

  async function dashboard(ctx: any): Promise<void> {
    let projectId = await chooseProject(ctx);
    if (!projectId) return;
    while (true) {
      let status: JsonObject;
      try {
        status = await call(ctx.cwd, ["status", projectId]);
      } catch (error) {
        ctx.ui.notify(friendlyError(error), "error");
        return;
      }
      const project = status.project || {};
      const currentStatus = String(project.status || "");
      const primaryAction = runAction(project);
      const actions = ["Статус проекта", "Предварительная проверка без LLM"];
      if (primaryAction) actions.push(primaryAction);
      if (currentStatus === "requirements_pending") {
        actions.push("Ответить на сформированные вопросы", "Утвердить требования");
      }
      if (currentStatus === "design_pending") {
        actions.push("Утвердить проект решения", "Вернуть проект решения на доработку");
      }
      if (currentStatus === "feedback_pending") {
        actions.push("Подтвердить итог: всё устраивает", "Отправить итог на доработку", "Сохранить как черновик");
      }
      if (currentStatus === "successful") actions.push("Создать новую ревизию итогового ответа");
      actions.push("Задать отдельный вопрос проекту", "Выбрать другой проект", "Закрыть");

      const action = await ctx.ui.select(`NewAgent · ${projectId} · ${currentStatus}`, actions);
      if (!action || action === "Закрыть") return;
      if (action === "Выбрать другой проект") {
        const selected = await chooseProject(ctx); if (selected) projectId = selected; continue;
      }
      try {
        if (action === "Статус проекта") {
          ctx.ui.notify(statusText(project), "info");
        } else if (action === "Предварительная проверка без LLM") {
          const result = await call(ctx.cwd, ["preflight", projectId]);
          ctx.ui.notify(
            `Preflight сохранён: ${result.path}\nLLM-вызовов: 0\nPython-скиллы: ${result.skill_runtime?.execution || "проверены"}`,
            "info"
          );
        } else if (primaryAction && action === primaryAction) {
          const confirm = await ctx.ui.select(`Запустить «${primaryAction}» через Wormsoft API?`, ["Запустить", "Отмена"]);
          if (confirm === "Запустить") {
            ctx.ui.notify(
              `Запуск начат: ${primaryAction}.\nПодготовка графа и ответ Wormsoft могут занять несколько минут. Не запускайте этап повторно.`,
              "info"
            );
            const result = await call(ctx.cwd, ["run", projectId]);
            ctx.ui.notify(`Артефакт сохранён: ${result.artifact}\nНовый статус: ${result.status}`, "info");
          }
        } else if (action === "Ответить на сформированные вопросы") {
          const questions = await call(ctx.cwd, ["questions", projectId]) as JsonObject[];
          for (const question of questions) {
            const options = (question.options || []).map((item: any) => String(item));
            let answer: string | undefined;
            if (options.length) {
              const selected = await ctx.ui.select(`${question.id}: ${question.text}`, [...options, "Другой ответ"]);
              answer = selected === "Другой ответ"
                ? await ctx.ui.input(`${question.id}: свой вариант`, "Введите точное бизнес-решение")
                : selected;
            } else {
              answer = await ctx.ui.input(`${question.id}: ${question.text}`, "Введите точный ответ");
            }
            if (answer?.trim()) await call(ctx.cwd, ["answer", projectId, "--set", `${question.id}=${answer.trim()}`]);
          }
          ctx.ui.notify("Ответы сохранены. Теперь отдельно утвердите требования.", "info");
        } else if (action === "Утвердить требования" || action === "Утвердить проект решения" || action === "Подтвердить итог: всё устраивает") {
          const stage = action === "Утвердить требования" ? "requirements" : action === "Утвердить проект решения" ? "design" : "instruction";
          const confirmation = await ctx.ui.select(
            `Подтвердить этап ${stage}? Это действие будет записано в аудит проекта.`,
            ["Утвердить", "Отмена"]
          );
          if (confirmation === "Утвердить") {
            const evidence = stage === "requirements"
              ? "Все показанные вопросы отвечены; требования утверждены пользователем Herdr/Pi"
              : stage === "design"
                ? "Проект решения утверждён пользователем Herdr/Pi"
                : "Всё устраивает; итог подтверждён пользователем Herdr/Pi";
            await call(ctx.cwd, ["approve", projectId, stage, "--by", "Пользователь Herdr/Pi", "--evidence", evidence]);
            ctx.ui.notify(`Этап ${stage} утверждён.`, "info");
          }
        } else if (action === "Вернуть проект решения на доработку") {
          const reason = await ctx.ui.input("Что изменить в проекте решения?", "Требования останутся утверждёнными");
          if (reason?.trim()) await call(ctx.cwd, ["revise-design", projectId, "--reason", reason.trim(), "--by", "Пользователь Pi"]);
        } else if (action === "Отправить итог на доработку" || action === "Создать новую ревизию итогового ответа") {
          const reason = await ctx.ui.input("Что исправить?", "Затронутые approvals будут отозваны");
          if (reason?.trim()) await call(ctx.cwd, ["request-changes", projectId, "--reason", reason.trim(), "--by", "Пользователь Pi"]);
        } else if (action === "Сохранить как черновик") {
          await call(ctx.cwd, ["save-draft", projectId]);
        } else if (action === "Задать отдельный вопрос проекту") {
          const selectedKind = await ctx.ui.select("Формат", [
            "process — описание процесса", "consultant — куда нажимать",
            "vanessa — сценарий Vanessa", "implementation — реализация"
          ]);
          const question = await ctx.ui.input("Вопрос по проекту", "Ответ сохранится в answers_md");
          if (!selectedKind || !question?.trim()) continue;
          const kind = selectedKind.split(" ", 1)[0];
          const confirm = await ctx.ui.select("Отправить один запрос внешней API-роли?", ["Отправить", "Отмена"]);
          if (confirm === "Отправить") {
            const answer = await call(ctx.cwd, ["ask", projectId, question.trim(), "--kind", kind]);
            ctx.ui.notify(`Ответ сохранён: ${answer.path}`, "info");
          }
        }
      } catch (error) {
        ctx.ui.notify(friendlyError(error), "error");
      }
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.hasUI) ctx.ui.notify("NewAgent Python runtime готов. Введите /erp.", "info");
  });
  pi.registerCommand("erp", { description: "Открыть кнопочный NewAgent workspace", handler: async (_args, ctx) => dashboard(ctx) });
  pi.registerCommand("erp-status", { description: "Проверить Python/API runtime", handler: async (_args, ctx) => ctx.ui.notify(JSON.stringify(await call(ctx.cwd, ["runtime-status"]), null, 2), "info") });
}
