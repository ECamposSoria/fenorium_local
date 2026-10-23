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
  return timingSafeEqual(Buffer.from(actual), Buffer.from(expected));
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

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    const chunks = [];
    let size = 0;
    let tooLarge = false;
    req.on('data', (chunk) => {
      if (tooLarge) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
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

let runtimePromise;
let runtimeModel;

async function optionalQvac(env = process.env) {
  if (env.FENORIUM_ASSISTANT_DISABLE_QVAC === '1') return null;
  if (runtimePromise) return runtimePromise;
  runtimePromise = (async () => {
    try {
      const sdk = await import('@qvac/sdk');
      runtimeModel = await sdk.loadModel({
        modelSrc: sdk.QWEN3_1_7B_INST_Q4,
        modelConfig: { ctx_size: 8192 },
      });
      return { sdk, modelId: runtimeModel };
    } catch {
      runtimeModel = undefined;
      return null;
    }
  })();
  return runtimePromise;
}

async function makeExplanation(request, env) {
  const runtime = await optionalQvac(env);
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

export function createAssistantServer({ env = process.env, pairingToken = createPairingToken(env) } = {}) {
  const origins = allowedOrigins(env.FENORIUM_ASSISTANT_ORIGINS);
  return createServer(async (req, res) => {
    const origin = req.headers.origin;
    const corsOkay = setCors(res, origin, origins);
    if (!corsOkay) return json(res, 403, { error: 'origin not allowed' });

    const url = new URL(req.url || '/', 'http://127.0.0.1');
    if (req.method === 'OPTIONS') {
      if (url.pathname !== '/ask') return json(res, 404, { error: 'not found' });
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
    if (req.method !== 'POST' || url.pathname !== '/ask') return json(res, 404, { error: 'not found' });
    if (req.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      return json(res, 415, { error: 'application/json required' });
    }
    if (!sameSecret(req.headers['x-fenorium-pairing-token'], pairingToken)) {
      return json(res, 401, { error: 'pairing token required' });
    }
    try {
      const body = JSON.parse(await readBody(req));
      const request = validateAskRequest(body);
      return json(res, 200, await makeExplanation(request, env));
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
