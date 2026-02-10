import type { Command } from "commander";
import {
  tokenGenerateCommand,
  tokenShowCommand,
  setupTokenAddCommand,
  setupTokenListCommand,
  setupTokenRemoveCommand,
} from "../../commands/token.js";
import { defaultRuntime } from "../../runtime.js";
import { formatDocsLink } from "../../terminal/links.js";
import { theme } from "../../terminal/theme.js";
import { runCommandWithRuntime } from "../cli-utils.js";

export function registerTokenCommand(program: Command) {
  const token = program
    .command("token")
    .description("Manage gateway authentication tokens")
    .addHelpText(
      "after",
      () =>
        `\n${theme.muted("Docs:")} ${formatDocsLink("/cli/token", "docs.openclaw.ai/cli/token")}\n`,
    );

  token
    .command("generate")
    .description("Generate a new secure gateway authentication token")
    .option("--save", "Save the token to openclaw.json config file", false)
    .option("--length <bytes>", "Token length in bytes (default: 32, min: 16, max: 128)", "32")
    .action(async (opts) => {
      await runCommandWithRuntime(defaultRuntime, async () => {
        const length = parseInt(opts.length as string, 10);
        if (isNaN(length)) {
          console.error("Error: --length must be a number");
          process.exitCode = 1;
          return;
        }
        await tokenGenerateCommand(
          {
            save: Boolean(opts.save),
            length,
          },
          defaultRuntime,
        );
      });
    });

  token
    .command("show")
    .description("Display the current gateway authentication token")
    .option("--json", "Output as JSON", false)
    .action(async (opts) => {
      await runCommandWithRuntime(defaultRuntime, async () => {
        await tokenShowCommand(
          {
            json: Boolean(opts.json),
          },
          defaultRuntime,
        );
      });
    });

  // Setup token management commands
  const setup = token
    .command("setup")
    .description("Manage Anthropic setup tokens for Claude API access");

  setup
    .command("add <token>")
    .description("Add an Anthropic setup token (starts with sk-ant-oat01-)")
    .option("--name <name>", "Profile name (default: 'default')")
    .option("--json", "Output as JSON", false)
    .action(async (tokenValue: string, opts) => {
      await runCommandWithRuntime(defaultRuntime, async () => {
        await setupTokenAddCommand(
          tokenValue,
          {
            name: opts.name as string | undefined,
            json: Boolean(opts.json),
          },
          defaultRuntime,
        );
      });
    });

  setup
    .command("list")
    .description("List configured Anthropic setup tokens")
    .option("--json", "Output as JSON", false)
    .action(async (opts) => {
      await runCommandWithRuntime(defaultRuntime, async () => {
        await setupTokenListCommand(
          {
            json: Boolean(opts.json),
          },
          defaultRuntime,
        );
      });
    });

  setup
    .command("remove <profile-id>")
    .description("Remove an Anthropic setup token by profile ID")
    .option("--json", "Output as JSON", false)
    .action(async (profileId: string, opts) => {
      await runCommandWithRuntime(defaultRuntime, async () => {
        await setupTokenRemoveCommand(
          profileId,
          {
            json: Boolean(opts.json),
          },
          defaultRuntime,
        );
      });
    });
}
