// Fetches the assistant's language model once, so the first question does not wait for a download.
// `make model` runs this in the assistant container; the model lands in the container's home,
// which Compose keeps on a volume.
const MODELS = { 'qwen3-1.7b': 'QWEN3_1_7B_INST_Q4', 'qwen3-4b': 'QWEN3_4B_INST_Q4_K_M', 'qwen3-8b': 'QWEN3_8B_INST_Q4_K_M' };
const name = process.env.FENORIUM_ASSISTANT_MODEL || 'qwen3-1.7b';
if (!MODELS[name]) {
  console.error(`unknown FENORIUM_ASSISTANT_MODEL ${name}; use one of ${Object.keys(MODELS).join(', ')}`);
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
const modelId = await sdk.loadModel({ modelSrc: sdk[MODELS[name]], modelConfig: { ctx_size: 8192 } });
console.log(`model ready in ${Math.round((Date.now() - started) / 1000)} s`);
await sdk.unloadModel({ modelId });
process.exit(0);
