// Loopback-only local assistant service. Validation and deterministic selection happen before
// optional inference, so the application remains useful when the QVAC runtime is not installed.
import { createServer } from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  ASSISTANT_SCHEMA_VERSION,
  assertValidEnvelope,
  buildExplanationPrompt,
  explainEnvelope,
  checkGrounding,
} from './tools.mjs';

export const DEFAULT_HOST = '127.0.0.1';
export const DEFAULT_PORT = 8787;
export const MAX_BODY_BYTES = 256 * 1024;
export const MAX_QUESTION_LENGTH = 2000;
export const DEFAULT_ORIGINS = Object.freeze([
  'http://localhost:3000',
  'http://127.0.0.1:3000',
  'http://localhost:5173',
  'http://127.0.0.1:5173',
  'http://localhost:8516',
  'http://127.0.0.1:8516',
  'https://app.fenorium.com',
]);

function originIsSafe(origin) {
  try {
    const parsed = new URL(origin);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') &&
      parsed.origin === origin && !origin.includes('*');
  } catch {
    return false;
  }
}

export function allowedOrigins(raw = process.env.FENORIUM_ASSISTANT_ORIGINS) {
  const values = raw === undefined
    ? DEFAULT_ORIGINS
    : String(raw).split(',').map((origin) => origin.trim()).filter(Boolean);
  if (!values.length || values.some((origin) => !originIsSafe(origin))) {
    throw new Error('FENORIUM_ASSISTANT_ORIGINS must be a non-empty explicit origin allowlist');
  }
  return Object.freeze([...new Set(values)]);
}

export function createPairingToken(env = process.env) {
  const configured = env.FENORIUM_ASSISTANT_PAIRING_TOKEN;
  const token = configured ? String(configured) : randomBytes(32).toString('base64url');
  if (token.length < 32 || token.length > 128 || !/^[A-Za-z0-9_-]+$/.test(token)) {
    throw new Error('assistant pairing token must be 32-128 URL-safe characters');
  }
  return token;
}

function sameSecret(actual, expected) {
  if (typeof actual !== 'string' || actual.length > 256 || actual.length !== expected.length) return false;
  const supplied = Buffer.from(actual);
  const wanted = Buffer.from(expected);
  return supplied.length === wanted.length && timingSafeEqual(supplied, wanted);
}

function json(res, status, payload) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(payload));
}

function setCors(res, origin, origins) {
  res.setHeader('Vary', 'Origin');
  if (origin && origins.includes(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Fenorium-Pairing-Token');
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    return true;
  }
  return !origin;
}

function readBody(req, maxBytes = MAX_BODY_BYTES) {
  return new Promise((resolveBody, reject) => {
    const chunks = [];
    let size = 0;
    let tooLarge = false;
    req.on('data', (chunk) => {
      if (tooLarge) return;
      size += chunk.length;
      if (size > maxBytes) {
        tooLarge = true;
        req.resume();
        reject(Object.assign(new Error('request body exceeds the bounded limit'), { statusCode: 413 }));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (!tooLarge) resolveBody(Buffer.concat(chunks).toString('utf8'));
    });
    req.on('error', reject);
  });
}
function parsePort(value) {
  const port = value === undefined ? DEFAULT_PORT : Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('invalid assistant port');
  return port;
}

function validateAskRequest(body) {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) throw new Error('JSON object required');
  if (Object.prototype.hasOwnProperty.call(body, 'portfolio') ||
      Object.prototype.hasOwnProperty.call(body, 'positions') ||
      Object.prototype.hasOwnProperty.call(body, 'holdings')) {
    throw new Error('raw portfolio data is not accepted');
  }
  const accepted = new Set(['question', 'envelope', 'field_refs']);
  if (Object.keys(body).some((key) => !accepted.has(key))) throw new Error('unsupported request field');
  if (typeof body.question !== 'string' || body.question.trim().length === 0 ||
      body.question.length > MAX_QUESTION_LENGTH) throw new Error('question is missing or too large');
  assertValidEnvelope(body.envelope);
  const deterministic = explainEnvelope({ envelope: body.envelope, field_refs: body.field_refs });
  return { question: body.question.trim(), envelope: body.envelope, fieldRefs: deterministic.field_refs, deterministic };
}

export function modelSettings(env = process.env) {
  const name = env.FENORIUM_ASSISTANT_MODEL || 'qwen3-1.7b';
  // Registry names for the models QVAC ships; Hugging Face URLs (checked against the Hub's SHA-256)
  // for the larger Qwen3 models it does not.
  const sources = { 'qwen3-1.7b': 'QWEN3_1_7B_INST_Q4', 'qwen3-4b': 'QWEN3_4B_INST_Q4_K_M',
    'qwen3-8b': 'QWEN3_8B_INST_Q4_K_M',
    'qwen3-14b': 'https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf',
    'qwen3-30b-a3b': 'https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf' };
  if (!Object.hasOwn(sources, name)) throw new Error('invalid FENORIUM_ASSISTANT_MODEL');
  const ctxSize = Number(env.FENORIUM_ASSISTANT_CTX ?? 8192);
  if (!Number.isSafeInteger(ctxSize) || ctxSize < 1) throw new Error('invalid FENORIUM_ASSISTANT_CTX');
  const device = env.FENORIUM_ASSISTANT_DEVICE ?? 'auto';
  if (!['auto', 'cpu'].includes(device)) throw new Error('invalid FENORIUM_ASSISTANT_DEVICE');
  const thinking = env.FENORIUM_ASSISTANT_THINKING ?? '0';
  if (!['0', '1'].includes(thinking)) throw new Error('invalid FENORIUM_ASSISTANT_THINKING');
  return { source: sources[name], ctxSize, device, thinking: thinking === '1' };
}

// A registry model carries its engine; a URL model names it, and its download must match the
// checksum Hugging Face publishes.
export function modelLoadOptions(settings, sdk) {
  const url = settings.source.startsWith('https://');
  return {
    modelSrc: url ? settings.source : sdk[settings.source],
    ...(url ? { modelType: 'llamacpp-completion', requireHttpChecksum: true } : {}),
    modelConfig: { ctx_size: settings.ctxSize, ...(settings.device === 'cpu' ? { device: 'cpu' } : {}) },
  };
}

function validateChatRequest(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body) ||
      Object.keys(body).some(key => !['system', 'messages', 'max_tokens'].includes(key))) {
    throw new Error('unsupported request field');
  }
  if (typeof body.system !== 'string' || body.system.length > 60000) throw new Error('invalid system');
  if (!Array.isArray(body.messages) || body.messages.length < 1 || body.messages.length > 24 ||
      body.messages.some(message => !message || typeof message !== 'object' ||
        Object.keys(message).some(key => !['role', 'content'].includes(key)) ||
        !['user', 'assistant'].includes(message.role) || typeof message.content !== 'string' ||
        !message.content.trim() || message.content.length > 8000) ||
      body.messages.at(-1).role !== 'user') throw new Error('invalid messages');
  if (!Number.isInteger(body.max_tokens) || body.max_tokens < 1 || body.max_tokens > 2048) {
    throw new Error('invalid max_tokens');
  }
  return body;
}

// Keep partial tags between events so neither reasoning nor split tags reach the client.
function thinkingFilter() {
  let pending = '', thinking = false;
  return (text, final = false) => {
    pending += text;
    let visible = '';
    while (pending) {
      const tag = thinking ? '</think>' : '<think>';
      const index = pending.indexOf(tag);
      if (index >= 0) {
        if (!thinking) visible += pending.slice(0, index);
        pending = pending.slice(index + tag.length);
        thinking = !thinking;
        continue;
      }
      let keep = 0;
      if (!final) {
        for (let count = 1; count < tag.length; count += 1) {
          if (pending.endsWith(tag.slice(0, count))) keep = count;
        }
      }
      if (!thinking) visible += pending.slice(0, pending.length - keep);
      pending = keep ? pending.slice(-keep) : '';
      break;
    }
    return visible;
  };
}

async function streamChat(req, res, request, getRuntime, settings) {
  let run, iterator, runtime, disconnected = false, cancelIssued = false, cancelRetried = false;
  const cancelRun = () => {
    if (!run?.requestId) return;
    if (cancelIssued) {
      if (!disconnected || cancelRetried) return;
      cancelRetried = true;
    } else cancelIssued = true;
    try { Promise.resolve(runtime?.sdk?.cancel?.({ requestId: run.requestId })).catch(() => {}); }
    catch { /* optional cancellation */ }
  };
  const onClose = () => {
    if (disconnected) return;
    disconnected = true;
    cancelRun();
  };
  res.on('close', onClose);
  req.on('aborted', onClose);
  try {
    runtime = await getRuntime();
    if (disconnected) return;
    if (!runtime) return json(res, 502, { error: 'assistant unavailable' });
    const history = [{ role: 'system', content: request.system },
      ...request.messages.map(message => ({ ...message }))];
    if (!settings.thinking) history.at(-1).content += '\n/no_think';
    const isOverflow = (error, event = false) =>
      (typeof runtime.sdk.ContextOverflowError === 'function' && error instanceof runtime.sdk.ContextOverflowError) ||
      error?.name === 'ContextOverflowError' ||
      (event && /context.*(overflow|exceed)/i.test(String(error?.message || '')));
    const emit = event => {
      if (disconnected) return;
      if (!res.headersSent) {
        res.writeHead(200, { 'Content-Type': 'application/x-ndjson; charset=utf-8',
          'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' });
        res.flushHeaders();
      }
      res.write(JSON.stringify(event) + '\n');
    };
    for (let attempt = 0; attempt < request.messages.length && !disconnected; attempt += 1) {
      run = undefined;
      iterator = undefined;
      cancelIssued = false;
      cancelRetried = false;
      try {
        run = runtime.sdk.completion({ modelId: runtime.modelId, history: [...history], stream: true,
          captureThinking: true, generationParams: { predict: request.max_tokens, temp: 0.4 } });
        if (disconnected) cancelRun();
        const finalOutcome = Promise.resolve(run.final).then(() => ({}), error => ({ error }));
        const finalFailure = finalOutcome.then(outcome => outcome.error
          ? { failure: outcome.error } : new Promise(() => {}));
        const filter = thinkingFilter();
        iterator = run.events[Symbol.asyncIterator]();
        let completed = false;
        while (true) {
          const next = await Promise.race([iterator.next(), finalFailure]);
          if (next.failure) throw next.failure;
          if (next.done) {
            const outcome = await finalOutcome;
            if (outcome.error) throw outcome.error;
            break;
          }
          const event = next.value;
          if (disconnected) cancelRun();
          if (event.type === 'completionDone') {
            if (event.stopReason === 'error' && isOverflow(event.error, true)) {
              throw Object.assign(new Error(event.error.message), { name: 'ContextOverflowError' });
            }
            const outcome = await finalOutcome;
            if (outcome.error) throw outcome.error;
            completed = true;
            if (!disconnected) {
              const text = filter('', true);
              if (text) emit({ type: 'delta', text });
              if (event.stopReason === 'error') {
                emit({ type: 'error', message: String(event.error?.message || 'Generation failed') });
              } else {
                emit({ type: 'done', finish: event.stopReason === 'length' ? 'length' : 'stop' });
              }
            }
            break;
          }
          if (disconnected) continue;
          if (event.type === 'contentDelta') {
            const text = filter(String(event.text || ''));
            if (text) emit({ type: 'delta', text });
          }
        }
        if (!disconnected && !completed) emit({ type: 'error', message: 'Generation ended without completion' });
        return;
      } catch (error) {
        if (disconnected) return;
        if (!isOverflow(error) || res.headersSent) throw error;
        if (history.length <= 2) return json(res, 413, { error: 'context_overflow' });
        history.splice(1, history[2]?.role === 'assistant' ? 2 : 1);
      } finally {
        try { Promise.resolve(iterator?.return?.()).catch(() => {}); } catch { /* optional event cleanup */ }
        iterator = undefined;
      }
    }
  } catch {
    if (!disconnected) {
      if (!res.headersSent) json(res, 502, { error: 'assistant unavailable' });
      else res.write(JSON.stringify({ type: 'error', message: 'Generation failed' }) + '\n');
    }
  } finally {
    try { Promise.resolve(iterator?.return?.()).catch(() => {}); } catch { /* optional event cleanup */ }
    res.off('close', onClose);
    req.off('aborted', onClose);
    if (!disconnected) res.end();
  }
}

let runtimePromise;
let runtimeModel;

async function optionalQvac(env = process.env) {
  if (env.FENORIUM_ASSISTANT_DISABLE_QVAC === '1') return null;
  if (runtimePromise) return runtimePromise;
  runtimePromise = (async () => {
    try {
      const sdk = await import('@qvac/sdk');
      runtimeModel = await sdk.loadModel(modelLoadOptions(modelSettings(env), sdk));
      return { sdk, modelId: runtimeModel };
    } catch {
      runtimeModel = undefined;
      return null;
    }
  })();
  return runtimePromise;
}

async function makeExplanation(request, getRuntime) {
  const runtime = await getRuntime();
  if (!runtime) return { assistant_status: 'assistant-unavailable', deterministic: request.deterministic };
  try {
    const run = runtime.sdk.completion({
      modelId: runtime.modelId,
      history: [{ role: 'user', content: buildExplanationPrompt(request.question, request.envelope, request.fieldRefs) }],
      stream: true,
    });
    const text = String((await run.text) || '').trim();
    const grounding = checkGrounding(text, request.envelope, request.fieldRefs);
    if (!grounding.ok) {
      return { assistant_status: 'available', explanation_status: 'rejected', grounding: grounding.detail,
        deterministic: request.deterministic };
    }
    return { assistant_status: 'available', explanation_status: 'grounded', explanation: text,
      field_refs: request.fieldRefs, deterministic: request.deterministic };
  } catch {
    return { assistant_status: 'assistant-unavailable', deterministic: request.deterministic };
  }
}

export function createAssistantServer({ env = process.env, pairingToken = createPairingToken(env), runtime } = {}) {
  const settings = modelSettings(env);
  const getRuntime = () => runtime === undefined ? optionalQvac(env) : Promise.resolve(runtime);
  let busy = false;
  const origins = allowedOrigins(env.FENORIUM_ASSISTANT_ORIGINS);
  return createServer(async (req, res) => {
    const origin = req.headers.origin;
    const corsOkay = setCors(res, origin, origins);
    if (!corsOkay) return json(res, 403, { error: 'origin not allowed' });

    const url = new URL(req.url || '/', 'http://127.0.0.1');
    if (req.method === 'OPTIONS') {
      if (!['/ask', '/chat'].includes(url.pathname)) return json(res, 404, { error: 'not found' });
      const requestedMethod = req.headers['access-control-request-method'];
      if (requestedMethod && requestedMethod !== 'POST') return json(res, 405, { error: 'method not allowed' });
      const requestedHeaders = String(req.headers['access-control-request-headers'] || '')
        .split(',').map((header) => header.trim().toLowerCase()).filter(Boolean);
      const allowedHeaders = new Set(['content-type', 'x-fenorium-pairing-token']);
      if (requestedHeaders.some((header) => !allowedHeaders.has(header))) {
        return json(res, 400, { error: 'request header not allowed' });
      }
      res.writeHead(204);
      return res.end();
    }
    if (req.method === 'GET' && url.pathname === '/health') {
      return json(res, 200, { ok: true, service: 'fenorium-assistant', schema_version: ASSISTANT_SCHEMA_VERSION });
    }
    if (req.method !== 'POST' || !['/ask', '/chat'].includes(url.pathname)) return json(res, 404, { error: 'not found' });
    if (req.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      return json(res, 415, { error: 'application/json required' });
    }
    if (!sameSecret(req.headers['x-fenorium-pairing-token'], pairingToken)) {
      return json(res, 401, { error: 'pairing token required' });
    }
    try {
      const body = JSON.parse(await readBody(req, url.pathname === '/chat' ? 2 * 1024 * 1024 : MAX_BODY_BYTES));
      const request = url.pathname === '/chat' ? validateChatRequest(body) : validateAskRequest(body);
      if (busy) return json(res, 503, { error: 'busy' });
      busy = true;
      try {
        if (url.pathname === '/chat') return await streamChat(req, res, request, getRuntime, settings);
        return json(res, 200, await makeExplanation(request, getRuntime));
      } finally { busy = false; }
    } catch (error) {
      const status = error?.statusCode || 400;
      return json(res, status, { error: String(error.message || 'invalid request') });
    }
  });
}

export function startAssistantServer({ env = process.env } = {}) {
  const token = createPairingToken(env);
  const server = createAssistantServer({ env, pairingToken: token });
  const port = parsePort(env.FENORIUM_ASSISTANT_PORT);
  server.listen(port, DEFAULT_HOST, () => {
    // This is the only startup secret disclosure and is intentionally once per process. Request
    // bodies, prompts, and generated text are never written to stdout or stderr.
    process.stderr.write(`[fenorium-assistant] listening on http://${DEFAULT_HOST}:${port}\n`);
    process.stderr.write(`[fenorium-assistant] origins=${allowedOrigins(env.FENORIUM_ASSISTANT_ORIGINS).join(',')} pairing_token=${token}\n`);
  });
  return { server, token, port, host: DEFAULT_HOST };
}

const isMain = process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
if (isMain) {
  const started = startAssistantServer();
  process.on('SIGINT', async () => {
    if (runtimeModel && runtimePromise) {
      try {
        const runtime = await runtimePromise;
        await runtime?.sdk?.unloadModel?.({ modelId: runtimeModel });
      } catch { /* optional runtime cleanup */ }
    }
    started.server.close(() => process.exit(0));
  });
}
