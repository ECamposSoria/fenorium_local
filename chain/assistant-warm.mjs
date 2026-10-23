// Fetches the assistant's language model once, so the first Explain does not wait for a download.
// `make model` runs this in the assistant container; the model lands in the container's home,
// which Compose keeps on a volume.
const started = Date.now();
let sdk;
try {
  sdk = await import('@qvac/sdk');
} catch (error) {
  console.error(`the QVAC runtime is not available here: ${error.message}`);
  process.exit(2);
}
console.log('loading Qwen3 1.7B (instruct, Q4) through the QVAC runtime; the first run downloads about 1 GB…');
const modelId = await sdk.loadModel({ modelSrc: sdk.QWEN3_1_7B_INST_Q4, modelConfig: { ctx_size: 8192 } });
console.log(`model ready in ${Math.round((Date.now() - started) / 1000)} s`);
await sdk.unloadModel({ modelId });
process.exit(0);
