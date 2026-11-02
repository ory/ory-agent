import { randomBytes } from "node:crypto";
import { writeFileSync } from "node:fs";

const name = process.argv[2];

if (!name) {
  console.error("usage: node greet.mjs <name>");
  process.exit(2);
}

const proof = randomBytes(16).toString("hex");
writeFileSync("/tmp/ory-claude-code-probe", proof, { mode: 0o600 });
console.log(`Hello, ${name}! proof=${proof}`);
