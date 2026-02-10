import crypto from "node:crypto";
import type { RuntimeEnv } from "../runtime.js";
import { loadConfig, writeConfigFile } from "../config/config.js";
import { resolveStateDir } from "../config/paths.js";
import { theme } from "../terminal/theme.js";

export type TokenGenerateOptions = {
  save?: boolean;
  length?: number;
};

export type TokenShowOptions = {
  json?: boolean;
};

/**
 * Generate a secure random token for gateway authentication
 */
export function generateSecureToken(length: number = 32): string {
  return crypto.randomBytes(length).toString("hex");
}

/**
 * Generate and optionally save a gateway authentication token
 */
export async function tokenGenerateCommand(
  options: TokenGenerateOptions,
  runtime: RuntimeEnv,
): Promise<void> {
  const length = options.length ?? 32;

  if (length < 16 || length > 128) {
    runtime.error("Token length must be between 16 and 128 bytes");
    runtime.exit(1);
  }

  const token = generateSecureToken(length);

  if (options.save) {
    // Load config and save the token
    const config = loadConfig();
    config.gateway = config.gateway ?? {};
    config.gateway.auth = config.gateway.auth ?? {};
    config.gateway.auth.mode = "token";
    config.gateway.auth.token = token;

    writeConfigFile(config);

    runtime.log("Gateway token generated and saved to config");
    runtime.log("");
    runtime.log(`Token: ${theme.accent(token)}`);
    runtime.log("");
    runtime.log("The token has been saved to your openclaw.json config file.");
  } else {
    runtime.log("Gateway token generated:");
    runtime.log("");
    runtime.log(theme.accent(token));
    runtime.log("");
    runtime.log("To use this token, add it to your config:");
    runtime.log("");
    runtime.log(theme.muted("  OPENCLAW_GATEWAY_TOKEN=" + token));
    runtime.log("");
    runtime.log("Or add it to your openclaw.json:");
    runtime.log("");
    runtime.log(
      theme.muted(
        JSON.stringify(
          {
            gateway: {
              auth: {
                mode: "token",
                token: token,
              },
            },
          },
          null,
          2,
        ),
      ),
    );
    runtime.log("");
    runtime.log(`Use ${theme.accent("--save")} to automatically save to config.`);
  }
}

/**
 * Show the current gateway authentication token
 */
export async function tokenShowCommand(
  options: TokenShowOptions,
  runtime: RuntimeEnv,
): Promise<void> {
  const config = loadConfig();
  const token =
    config.gateway?.auth?.token ||
    process.env.OPENCLAW_GATEWAY_TOKEN ||
    process.env.CLAWDBOT_GATEWAY_TOKEN;

  if (!token) {
    if (options.json) {
      runtime.log(JSON.stringify({ token: null, configured: false }, null, 2));
    } else {
      runtime.log(theme.warn("No gateway token configured"));
      runtime.log("");
      runtime.log(`Generate a new token with: ${theme.accent("openclaw token generate")}`);
    }
    return;
  }

  if (options.json) {
    runtime.log(JSON.stringify({ token, configured: true }, null, 2));
  } else {
    runtime.log("Current gateway token:");
    runtime.log("");
    runtime.log(theme.accent(token));
    runtime.log("");
    const stateDir = resolveStateDir();
    const configPath = `${stateDir}/openclaw.json`;
    runtime.log(theme.muted(`Configured in: ${configPath}`));
  }
}
