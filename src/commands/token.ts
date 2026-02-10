import crypto from "node:crypto";
import type { RuntimeEnv } from "../runtime.js";
import {
  ensureAuthProfileStore,
  listProfilesForProvider,
  saveAuthProfileStore,
  upsertAuthProfile,
} from "../agents/auth-profiles.js";
import { normalizeProviderId } from "../agents/model-selection.js";
import { loadConfig, writeConfigFile } from "../config/config.js";
import { resolveStateDir } from "../config/paths.js";
import { theme } from "../terminal/theme.js";
import {
  validateAnthropicSetupToken,
  buildTokenProfileId,
  DEFAULT_TOKEN_PROFILE_NAME,
  ANTHROPIC_SETUP_TOKEN_PREFIX,
} from "./auth-token.js";

export type TokenGenerateOptions = {
  save?: boolean;
  length?: number;
};

export type TokenShowOptions = {
  json?: boolean;
};

export type SetupTokenAddOptions = {
  name?: string;
  json?: boolean;
};

export type SetupTokenListOptions = {
  json?: boolean;
};

export type SetupTokenRemoveOptions = {
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

/**
 * Add an Anthropic setup token to auth profiles
 */
export async function setupTokenAddCommand(
  token: string,
  options: SetupTokenAddOptions,
  runtime: RuntimeEnv,
): Promise<void> {
  const trimmedToken = token.trim();

  // Validate the token
  const error = validateAnthropicSetupToken(trimmedToken);
  if (error) {
    runtime.error(`Invalid setup token: ${error}`);
    runtime.exit(1);
  }

  const profileName = options.name || DEFAULT_TOKEN_PROFILE_NAME;
  const profileId = buildTokenProfileId({ provider: "anthropic", name: profileName });

  // Add to auth profiles
  upsertAuthProfile({
    profileId,
    credential: {
      type: "token",
      provider: "anthropic",
      token: trimmedToken,
    },
  });

  if (options.json) {
    runtime.log(
      JSON.stringify(
        {
          success: true,
          profileId,
          provider: "anthropic",
          name: profileName,
        },
        null,
        2,
      ),
    );
  } else {
    runtime.log("Anthropic setup token added successfully!");
    runtime.log("");
    runtime.log(`Profile ID: ${theme.accent(profileId)}`);
    runtime.log("");
    runtime.log("This token will be used for authenticating with Anthropic's API.");
    runtime.log("");
    runtime.log(`View all setup tokens with: ${theme.accent("openclaw token setup list")}`);
  }
}

/**
 * List configured Anthropic setup tokens
 */
export async function setupTokenListCommand(
  options: SetupTokenListOptions,
  runtime: RuntimeEnv,
): Promise<void> {
  const store = ensureAuthProfileStore(undefined, { allowKeychainPrompt: false });
  const anthropicProfiles = listProfilesForProvider(store, "anthropic");

  // Filter for setup tokens (token type starting with the setup token prefix)
  const setupTokenProfiles = anthropicProfiles.filter((profileId) => {
    const profile = store.profiles[profileId];
    if (profile?.type === "token") {
      return profile.token.startsWith(ANTHROPIC_SETUP_TOKEN_PREFIX);
    }
    return false;
  });

  if (options.json) {
    const profiles = setupTokenProfiles.map((profileId) => {
      const profile = store.profiles[profileId];
      return {
        profileId,
        provider: profile?.provider,
        type: profile?.type,
        tokenPrefix: profile?.type === "token" ? profile.token.substring(0, 20) + "..." : undefined,
      };
    });
    runtime.log(JSON.stringify({ profiles, count: profiles.length }, null, 2));
  } else {
    if (setupTokenProfiles.length === 0) {
      runtime.log(theme.warn("No Anthropic setup tokens configured"));
      runtime.log("");
      runtime.log(`Add a setup token with: ${theme.accent("openclaw token setup add <token>")}`);
      return;
    }

    runtime.log(`Found ${theme.accent(setupTokenProfiles.length.toString())} setup token(s):`);
    runtime.log("");

    for (const profileId of setupTokenProfiles) {
      const profile = store.profiles[profileId];
      if (profile?.type === "token") {
        const tokenPreview = profile.token.substring(0, 20) + "...";
        runtime.log(`  ${theme.accent(profileId)}`);
        runtime.log(`    Token: ${theme.muted(tokenPreview)}`);
        runtime.log("");
      }
    }

    runtime.log(`Remove a token with: ${theme.accent("openclaw token setup remove <profile-id>")}`);
  }
}

/**
 * Remove an Anthropic setup token from auth profiles
 */
export async function setupTokenRemoveCommand(
  profileId: string,
  options: SetupTokenRemoveOptions,
  runtime: RuntimeEnv,
): Promise<void> {
  const store = ensureAuthProfileStore(undefined, { allowKeychainPrompt: false });

  if (!store.profiles[profileId]) {
    runtime.error(`Profile "${profileId}" not found`);
    runtime.exit(1);
  }

  const profile = store.profiles[profileId];
  const provider = normalizeProviderId(profile.provider);

  if (
    provider !== "anthropic" ||
    profile.type !== "token" ||
    !profile.token.startsWith(ANTHROPIC_SETUP_TOKEN_PREFIX)
  ) {
    runtime.error(`Profile "${profileId}" is not an Anthropic setup token`);
    runtime.exit(1);
  }

  // Remove the profile
  delete store.profiles[profileId];

  // Clean up references in order and lastGood
  if (store.order?.[provider]) {
    store.order[provider] = store.order[provider].filter((id) => id !== profileId);
    if (store.order[provider].length === 0) {
      delete store.order[provider];
    }
  }

  if (store.lastGood?.[provider] === profileId) {
    delete store.lastGood[provider];
  }

  saveAuthProfileStore(store);

  if (options.json) {
    runtime.log(JSON.stringify({ success: true, removed: profileId }, null, 2));
  } else {
    runtime.log(`Removed setup token: ${theme.accent(profileId)}`);
    runtime.log("");
    runtime.log(`View remaining tokens with: ${theme.accent("openclaw token setup list")}`);
  }
}
