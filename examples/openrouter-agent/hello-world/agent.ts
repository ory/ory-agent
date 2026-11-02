import { randomUUID } from "node:crypto";
import { parseArgs } from "node:util";

import { createInitialState, OpenRouter, tool } from "@openrouter/agent";
import type { ConversationState } from "@openrouter/agent";
import { createOryHooks, OryAgentClient } from "@ory/openrouter-agent";
import { z } from "zod";

export const DEFAULT_PROMPT = "Use the Greet tool, then tell me what the tool returned.";
export const DEFAULT_MODEL = "openai/gpt-4.1-mini";

// OryAgentClient.fromEnv() is intentionally permissive for interactive development: an
// incomplete configuration can produce a disconnected, pass-through client or fall back to
// interactive authentication. A headless deployment must fail before either behavior is possible.
const REQUIRED_DEPLOYMENT_ENV = [
  "ORY_PROJECT_URL",
  "ORY_AGENT_SECURITY_URL",
  "ORY_AGENT_TYPE",
  "ORY_AGENT_CREDENTIAL_METHOD",
  "ORY_AGENT_API_KEY",
] as const;

/** Temporarily enforce the complete headless deployment-package contract. */
export function requireDeploymentEnvironment(env: NodeJS.ProcessEnv = process.env): void {
  const missing = REQUIRED_DEPLOYMENT_ENV.filter((name) => !env[name]);
  if (missing.length > 0) {
    throw new Error(`missing Agent Security deployment environment: ${missing.join(", ")}`);
  }
  if (env.ORY_AGENT_TYPE !== "openrouter-agent") {
    throw new Error("ORY_AGENT_TYPE must be openrouter-agent");
  }
  if (env.ORY_AGENT_CREDENTIAL_METHOD !== "talos_api_key") {
    throw new Error("ORY_AGENT_CREDENTIAL_METHOD must be talos_api_key");
  }
  if (!env.OPENROUTER_API_KEY) {
    throw new Error("OPENROUTER_API_KEY is required");
  }
}

requireDeploymentEnvironment();
const ory = OryAgentClient.fromEnv("openrouter-agent");
const openrouter = new OpenRouter({ apiKey: process.env.OPENROUTER_API_KEY });

export const greet = tool({
  name: "Greet",
  description: "Return a greeting for the supplied name.",
  inputSchema: z.object({ name: z.string() }),
  execute: async ({ name }) => `Hello, ${name}!`,
});

/** Run one complete guarded Agent turn and return its final output. */
export async function run(
  prompt: string = DEFAULT_PROMPT,
  sessionId: string = process.env.ORY_AGENT_SESSION_ID || randomUUID(),
): Promise<string> {
  // OpenRouter exposes the conversation ID as the hook session ID. Seed it so the Agent
  // Security session, tool activity, and session.end activity share one opaque identifier.
  let state: ConversationState | null = createInitialState(sessionId);
  const activityOffset = ory.logger.activities().length;
  let status: "ok" | "error" = "error";
  let output: string;
  try {
    const result = openrouter.callModel({
      model: process.env.OPENROUTER_MODEL || DEFAULT_MODEL,
      instructions:
        "Call Greet exactly once with name='world'. After the tool returns, " +
        "give the user a concise final response containing its result.",
      input: prompt,
      tools: [greet] as const,
      // A forced choice is one-shot: after Greet is requested, the SDK relaxes it to auto
      // so the model can produce its final response.
      toolChoice: { type: "function", name: "Greet" },
      parallelToolCalls: false,
      state: {
        load: async () => state,
        save: async (next) => {
          state = next;
        },
      },
      hooks: createOryHooks({ client: ory }),
    });
    output = await result.getText();
    status = "ok";
  } finally {
    ory.logger.activity("session.end", status, {
      sessionId,
      attributes: { harness: "openrouter-agent" },
    });
    await ory.flushEvents(3_000);
  }

  // Unlike the Python client, the TypeScript flush does not report whether every event was
  // accepted, so surface the privacy-safe failure codes Argus recorded during this run.
  const errors = new Set<unknown>();
  for (const activity of ory.logger.activities().slice(activityOffset)) {
    if (activity.status !== "error" || !activity.attributes) continue;
    for (const code of [activity.attributes.error, activity.attributes.reasonCode]) {
      if (code) errors.add(code);
    }
  }
  if (errors.has("session_inactive")) {
    throw new Error(
      "Agent Security rejected the deployment credential; it may be invalid, " +
        "expired, or revoked. Create and inject a new deployment package.",
    );
  }
  if (errors.has("tls_error")) {
    throw new Error(
      "Agent Security TLS verification failed. For Local Lite, set NODE_EXTRA_CA_CERTS " +
        "to the active Caddy CA described in README.md.",
    );
  }
  if (errors.has("network_error")) {
    throw new Error(
      "Agent Security could not be reached. Verify ORY_AGENT_SECURITY_URL and the deployment network path.",
    );
  }
  return output;
}

async function main(): Promise<void> {
  const { positionals, values } = parseArgs({
    allowPositionals: true,
    options: { "session-id": { type: "string" } },
  });
  console.log(await run(positionals[0] ?? DEFAULT_PROMPT, values["session-id"]));
}

if (import.meta.main) {
  await main();
}
