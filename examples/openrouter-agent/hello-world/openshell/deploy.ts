import { execFile as execFileCallback, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs, promisify } from "node:util";

const execFile = promisify(execFileCallback);

export const EXAMPLE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PROFILES = {
  openrouter: path.join(EXAMPLE_ROOT, "openshell", "profiles", "openrouter.yaml"),
  "ory-agent-security": path.join(EXAMPLE_ROOT, "openshell", "profiles", "ory-agent-security.yaml"),
} as const;
const REQUIRED_ORY_ENV = [
  "ORY_PROJECT_URL",
  "ORY_AGENT_SECURITY_URL",
  "ORY_AGENT_TYPE",
  "ORY_AGENT_CREDENTIAL_METHOD",
  "ORY_AGENT_API_KEY",
] as const;
const COMPATIBILITY_ORY_ENV = ["ORY_AGENT_SUBJECT_ID", "ORY_AGENT_ROOT_DELEGATION"] as const;
const MAX_CREDENTIAL_BYTES = 1024 * 1024;
// Everything except the Talos key is routing and type configuration.
const NON_SECRET_ORY_ENV = REQUIRED_ORY_ENV.filter((name) => name !== "ORY_AGENT_API_KEY");
export const AGENT_COMMAND = ["node", "/app/agent.ts"];

type OryEnv = Record<(typeof REQUIRED_ORY_ENV)[number], string>;

export interface Runner {
  /** Run a command, capturing output; rejects on a non-zero exit. */
  capture(command: string, args: string[], env?: NodeJS.ProcessEnv): Promise<string>;
  /** Run a command with inherited stdio and return its exit code. */
  stream(command: string, args: string[]): Promise<number>;
}

export const defaultRunner: Runner = {
  async capture(command, args, env) {
    const { stdout } = await execFile(command, args, {
      env: env ? { ...process.env, ...env } : process.env,
      maxBuffer: 16 * 1024 * 1024,
    });
    return stdout;
  },
  stream(command, args) {
    return new Promise((resolve, reject) => {
      const child = spawn(command, args, { stdio: "inherit" });
      child.once("error", reject);
      child.once("exit", (code, signal) => resolve(code ?? (signal ? 128 : 1)));
    });
  },
};

export function parseCredentials(contents: string): OryEnv {
  const known = new Set<string>([...REQUIRED_ORY_ENV, ...COMPATIBILITY_ORY_ENV]);
  const values = new Map<string, string>();
  contents.split(/\r?\n/).forEach((rawLine, index) => {
    const lineNumber = index + 1;
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) return;
    const separator = line.indexOf("=");
    if (separator < 0) throw new Error(`invalid credential entry on line ${lineNumber}`);
    const name = line.slice(0, separator);
    if (!known.has(name)) {
      throw new Error(`unexpected credential key on line ${lineNumber}: ${name}`);
    }
    let value: unknown;
    try {
      value = JSON.parse(line.slice(separator + 1));
    } catch {
      throw new Error(`invalid credential value on line ${lineNumber}: ${name}`);
    }
    if (typeof value !== "string" || !value || values.has(name)) {
      throw new Error(`invalid credential value on line ${lineNumber}: ${name}`);
    }
    values.set(name, value);
  });

  const missing = REQUIRED_ORY_ENV.filter((name) => !values.has(name));
  if (missing.length > 0) throw new Error(`deployment credentials are missing: ${missing.join(", ")}`);
  if (values.get("ORY_AGENT_TYPE") !== "openrouter-agent") {
    throw new Error("ORY_AGENT_TYPE must be openrouter-agent");
  }
  if (values.get("ORY_AGENT_CREDENTIAL_METHOD") !== "talos_api_key") {
    throw new Error("ORY_AGENT_CREDENTIAL_METHOD must be talos_api_key");
  }
  // The runtime resolves its subject and root delegation from Agent Security at startup, so the
  // package's compatibility values are deliberately dropped.
  return Object.fromEntries(REQUIRED_ORY_ENV.map((name) => [name, values.get(name)!])) as OryEnv;
}

async function readCredentialsStdin(): Promise<string> {
  if (process.stdin.isTTY) throw new Error("probe requires deployment credentials on stdin");
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += data.length;
    if (size > MAX_CREDENTIAL_BYTES) throw new Error("deployment credentials exceed 1 MiB");
    chunks.push(data);
  }
  return Buffer.concat(chunks).toString("utf8");
}

export function hostname(origin: string, name: string): string {
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    throw new Error(`${name} must be an HTTPS origin`);
  }
  if (
    parsed.protocol !== "https:" ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    !["", "/"].includes(parsed.pathname) ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error(`${name} must be an HTTPS origin`);
  }
  return parsed.hostname;
}

/**
 * Build the deny-by-default sandbox policy. OpenRouter access comes from its provider profile;
 * Agent Security and the Ory project are added here because their hosts differ per deployment.
 */
export function sandboxPolicy(oryEnv: OryEnv, oryProvider: string): string {
  const agentSecurityHost = hostname(oryEnv.ORY_AGENT_SECURITY_URL, "ORY_AGENT_SECURITY_URL");
  const projectHost = hostname(oryEnv.ORY_PROJECT_URL, "ORY_PROJECT_URL");
  return [
    "version: 1",
    "filesystem_policy:",
    "  include_workdir: false",
    "  read_only: [/usr, /lib, /etc, /app, /proc, /dev/urandom]",
    "  read_write: [/tmp, /dev/null]",
    "landlock:",
    "  compatibility: hard_requirement",
    "network_policies:",
    "  ory_agent_security:",
    "    endpoints:",
    `      - host: ${JSON.stringify(agentSecurityHost)}`,
    "        port: 443",
    "        protocol: rest",
    "        access: full",
    "        enforcement: enforce",
    "        credential_binding:",
    `          provider: ${JSON.stringify(oryProvider)}`,
    "    binaries:",
    "      - path: /usr/local/bin/node",
    "  ory_project:",
    "    endpoints:",
    `      - host: ${JSON.stringify(projectHost)}`,
    "        port: 443",
    "        protocol: rest",
    "        access: full",
    "        enforcement: enforce",
    "    binaries:",
    "      - path: /usr/local/bin/node",
    "",
  ].join("\n");
}

/** Import the example's provider profiles unless the gateway already has them. */
export async function setup(runner: Runner = defaultRunner): Promise<Record<string, string>> {
  const result: Record<string, string> = {};
  for (const [id, file] of Object.entries(PROFILES)) {
    try {
      await runner.capture("openshell", ["profile", "export", id, "-o", "yaml"]);
      result[id] = "existing";
    } catch {
      await runner.capture("openshell", ["profile", "lint", "-f", file]);
      await runner.capture("openshell", ["profile", "import", "-f", file]);
      result[id] = "imported";
    }
  }
  return result;
}

export async function buildImage(
  tag: string,
  push: boolean = false,
  runner: Runner = defaultRunner,
): Promise<Record<string, string>> {
  await runner.capture("docker", [
    "build",
    "--file",
    path.join(EXAMPLE_ROOT, "openshell", "Dockerfile"),
    "--tag",
    tag,
    EXAMPLE_ROOT,
  ]);
  if (push) {
    await runner.capture("docker", ["push", tag]);
    const repository = tag.slice(0, tag.lastIndexOf(":") > tag.lastIndexOf("/") ? tag.lastIndexOf(":") : tag.length);
    let repoDigests: unknown;
    try {
      repoDigests = JSON.parse(
        await runner.capture("docker", ["image", "inspect", "--format", "{{json .RepoDigests}}", tag]),
      );
    } catch {
      throw new Error("registry did not return an immutable image digest");
    }
    const imageRef = Array.isArray(repoDigests)
      ? repoDigests.find(
          (digest): digest is string => typeof digest === "string" && digest.startsWith(`${repository}@sha256:`),
        )
      : undefined;
    if (!imageRef) throw new Error("registry did not return an immutable image digest");
    return { image_ref: imageRef };
  }
  return { image_ref: tag };
}

export async function loadImageRef(imageFile: string): Promise<string> {
  let artifact: unknown;
  try {
    artifact = JSON.parse(await readFile(imageFile, "utf8"));
  } catch {
    throw new Error(`invalid OpenShell image artifact: ${imageFile}`);
  }
  const imageRef =
    typeof artifact === "object" && artifact !== null && "image_ref" in artifact
      ? (artifact as { image_ref?: unknown }).image_ref
      : undefined;
  if (typeof imageRef !== "string" || !imageRef.trim()) {
    throw new Error(`invalid OpenShell image artifact: ${imageFile}`);
  }
  return imageRef.trim();
}

export async function runProbe(
  imageRef: string,
  credentials: string,
  runner: Runner = defaultRunner,
  env: NodeJS.ProcessEnv = process.env,
): Promise<Record<string, string>> {
  const oryEnv = parseCredentials(credentials);
  const openrouterApiKey = env.OPENROUTER_API_KEY;
  if (!openrouterApiKey) throw new Error("OPENROUTER_API_KEY is required");

  const suffix = randomBytes(4).toString("hex");
  const sandbox = `ory-openrouter-hello-world-${suffix}`;
  const openrouterProvider = `${sandbox}-openrouter`;
  const oryProvider = `${sandbox}-ory`;
  const workdir = await mkdtemp(path.join(tmpdir(), "ory-openshell-"));
  const policyFile = path.join(workdir, "policy.yaml");
  const ephemeralProviders = [openrouterProvider, oryProvider];
  try {
    await writeFile(policyFile, sandboxPolicy(oryEnv, oryProvider), { mode: 0o600 });

    // `--credential NAME` reads the value from the child environment, keeping secrets out of argv.
    // The gateway stores the real values; the sandbox only ever receives placeholders.
    await runner.capture(
      "openshell",
      [
        "provider",
        "create",
        "--name",
        openrouterProvider,
        "--type",
        "openrouter",
        "--credential",
        "OPENROUTER_API_KEY",
      ],
      { OPENROUTER_API_KEY: openrouterApiKey },
    );
    await runner.capture(
      "openshell",
      [
        "provider",
        "create",
        "--name",
        oryProvider,
        "--type",
        "ory-agent-security",
        "--credential",
        "ORY_AGENT_API_KEY",
      ],
      { ORY_AGENT_API_KEY: oryEnv.ORY_AGENT_API_KEY },
    );

    const exitCode = await runner.stream("openshell", [
      "sandbox",
      "create",
      "--name",
      sandbox,
      "--from",
      imageRef,
      "--policy",
      policyFile,
      "--provider",
      openrouterProvider,
      "--provider",
      oryProvider,
      "--no-auto-providers",
      "--label",
      "workload=ory-openrouter-agent-hello-world",
      ...NON_SECRET_ORY_ENV.flatMap((name) => ["--env", `${name}=${oryEnv[name]}`]),
      "--no-tty",
      "--no-keep",
      "--",
      ...AGENT_COMMAND,
    ]);
    if (exitCode !== 0) throw new Error(`OpenShell Agent probe failed with exit code ${exitCode}`);
    return { sandbox, image_ref: imageRef, status: "succeeded" };
  } finally {
    // --no-keep does not remove a sandbox that timed out while provisioning.
    await runner.capture("openshell", ["sandbox", "delete", sandbox]).catch(() => undefined);
    const providerCleanupErrors: unknown[] = [];
    for (const provider of ephemeralProviders) {
      try {
        await runner.capture("openshell", ["provider", "delete", provider]);
      } catch (error) {
        providerCleanupErrors.push(error);
      }
    }
    await rm(workdir, { recursive: true, force: true });
    if (providerCleanupErrors.length > 0) {
      throw new AggregateError(providerCleanupErrors, "failed to delete ephemeral OpenShell credential providers");
    }
  }
}

async function main(): Promise<void> {
  const [command, ...rest] = process.argv.slice(2);
  let output: Record<string, string>;
  if (command === "setup") {
    output = await setup();
  } else if (command === "build") {
    const { values } = parseArgs({
      args: rest,
      options: { tag: { type: "string" }, push: { type: "boolean", default: false }, output: { type: "string" } },
    });
    if (!values.tag) throw new Error("build requires --tag");
    output = await buildImage(values.tag, values.push);
    if (values.output) {
      await mkdir(path.dirname(values.output), { recursive: true });
      await writeFile(values.output, `${JSON.stringify(output, null, 0)}\n`);
      return;
    }
  } else if (command === "probe") {
    const { values } = parseArgs({
      args: rest,
      options: { "image-file": { type: "string" } },
    });
    if (!values["image-file"]) throw new Error("probe requires --image-file");
    output = await runProbe(await loadImageRef(values["image-file"]), await readCredentialsStdin());
  } else {
    throw new Error("usage: deploy.ts setup | build --tag TAG [--push] [--output FILE] | probe --image-file FILE");
  }
  console.log(JSON.stringify(output));
}

if (import.meta.main) {
  await main();
}
