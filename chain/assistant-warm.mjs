// Fetches the assistant's language model once, so the first question does not wait for a download.
// `make model` runs this in the assistant container; the model lands in the container's home,
// which Compose keeps on a volume. The model list and how each one loads come from the assistant.
import { modelLoadOptions, modelSettings } from '../assistant/serve.mjs';

const name = process.env.FENORIUM_ASSISTANT_MODEL || 'qwen3-1.7b';
let settings;
try {
  settings = modelSettings(process.env);
} catch (error) {
  console.error(`${error.message}: ${name}; use qwen3-1.7b, qwen3-4b, qwen3-8b, qwen3-14b or qwen3-30b-a3b`);
  process.exit(2);
}
const started = Date.now();
let sdk;
try {
  sdk = await import('@qvac/sdk');
} catch (error) {
  console.error(`the QVAC runtime is not available here: ${error.message}`);
  process.exit(2);
}
console.log(`loading ${name} through the QVAC runtime; the first run downloads it…`);
const modelId = await sdk.loadModel(modelLoadOptions(settings, sdk));
console.log(`model ready in ${Math.round((Date.now() - started) / 1000)} s`);
await sdk.unloadModel({ modelId });
process.exit(0);
