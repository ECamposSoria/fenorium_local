// CLI explanation path. Its input is an AssistantEnvelope, never a portfolio-shaped array.
import { readFileSync } from 'node:fs';
import {
  assertValidEnvelope,
  buildExplanationPrompt,
  explainEnvelope,
  checkGrounding,
} from './tools.mjs';

const [, , envelopePath, question] = process.argv;
if (!envelopePath || !question) {
  console.error('usage: node assistant/ask.mjs <assistant-envelope.json> "<question>"');
  process.exit(2);
}
if (question.length > 2000) {
  console.error('question is too large');
  process.exit(1);
}

let envelope;
try {
  envelope = JSON.parse(readFileSync(envelopePath, 'utf8'));
  assertValidEnvelope(envelope);
} catch (error) {
  console.error(`invalid AssistantEnvelope: ${error instanceof Error ? error.message : 'invalid input'}`);
  process.exit(1);
}

const deterministic = explainEnvelope({ envelope });
let modelId;
try {
  let sdk;
  try {
    if (process.env.FENORIUM_ASSISTANT_DISABLE_QVAC === '1') throw new Error('disabled');
    sdk = await import('@qvac/sdk');
    modelId = await sdk.loadModel({
      modelSrc: sdk.QWEN3_1_7B_INST_Q4,
      modelConfig: { ctx_size: 8192 },
    });
  } catch {
    console.log(JSON.stringify({ assistant_status: 'assistant-unavailable', deterministic }));
    process.exit(0);
  }

  try {
    const run = sdk.completion({
      modelId,
      history: [{ role: 'user', content: buildExplanationPrompt(question, envelope) }],
      stream: true,
    });
    const text = String((await run.text) || '').trim();
    const grounding = checkGrounding(text, envelope);
    if (!grounding.ok) {
      console.log(JSON.stringify({ assistant_status: 'available', explanation_status: 'rejected',
        grounding: grounding.detail, deterministic }));
    } else {
      console.log(JSON.stringify({ assistant_status: 'available', explanation_status: 'grounded',
        explanation: text, field_refs: deterministic.field_refs, deterministic }));
    }
  } catch {
    console.log(JSON.stringify({ assistant_status: 'assistant-unavailable', deterministic }));
  } finally {
    await sdk.unloadModel?.({ modelId }).catch(() => {});
  }
} finally {
  // No input, prompt, or runtime diagnostic is printed here.
}
