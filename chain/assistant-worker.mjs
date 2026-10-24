// The chat needs one QVAC plugin: text completion. The SDK's default worker registers all of
// them and loads every add-on's native code at start; this is the entry `qvac bundle sdk`
// generates for a single plugin.
import { initializeWorker, ensureRPCSetup } from "@qvac/sdk/worker-lifecycle";
import { registerPlugin } from "@qvac/sdk/plugins";
import { getServerLogger } from "@qvac/sdk/logging";
import { llmPlugin } from "@qvac/sdk/llamacpp-completion/plugin";

const { hasRPCConfig } = initializeWorker();
getServerLogger().info("QVAC worker: text completion only");
registerPlugin(llmPlugin);
if (hasRPCConfig) ensureRPCSetup();
