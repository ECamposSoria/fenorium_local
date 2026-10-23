// Pure, read-only assistant contract.
//
// The engine is the authority for every value in an AssistantEnvelope. This module only checks
// the envelope, selects already-present fields, and checks generated prose against those fields.
// It deliberately has no inference-runtime import so validation remains usable without QVAC.

export const ASSISTANT_SCHEMA_VERSION = 'fenorium/assistant-envelope/1';
export const MAX_FACTS = 256;
export const MAX_FIELD_REF_LENGTH = 256;
export const MAX_SOURCE_REF_LENGTH = 512;
export const MAX_VALUE_LENGTH = 2048;
export const MAX_ENVELOPE_BYTES = 128 * 1024;

const FIELD_REF = /^[a-z][a-z0-9_-]*(?:(?:[/:.])[A-Za-z0-9_-]+|\[[0-9]+\])+$/;
const SOURCE_REF = /^(?:[a-z][a-z0-9+.-]*:|[a-z][a-z0-9_-]*[/#])[\x21-\x7e]+$/;
const METHOD_VERSION = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const REASON_CODE = /^[A-Za-z][A-Za-z0-9_-]{0,127}$/;
const STATUS = /^[A-Za-z][A-Za-z0-9 _-]{0,63}$/;
const FACT_KEYS = new Set([
  'field_ref', 'status', 'reason_code', 'value', 'unit', 'source_ref', 'source_refs', 'label',
]);
const ENVELOPE_KEYS = new Set(['schema_version', 'method_version', 'as_of', 'facts']);

export class EnvelopeValidationError extends TypeError {}

function fail(message) {
  throw new EnvelopeValidationError(message);
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isUtcTimestamp(value) {
  const match = typeof value === 'string' && value.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?Z$/);
  if (!match) {
    return false;
  }
  const millis = (match[2] || '').slice(0, 3).padEnd(3, '0');
  const canonical = `${match[1]}.${millis}Z`;
  return Number.isFinite(Date.parse(value)) && new Date(canonical).toISOString() === canonical;
}

function validateFieldRef(value, where = 'field_ref') {
  if (typeof value !== 'string' || value.length > MAX_FIELD_REF_LENGTH || !FIELD_REF.test(value)) {
    fail(`${where} is not a stable field reference`);
  }
  return value;
}

function validateSourceRef(value, where) {
  if (typeof value !== 'string' || value.length > MAX_SOURCE_REF_LENGTH || !SOURCE_REF.test(value)) {
    fail(`${where} is not a valid source reference`);
  }
  return value;
}

function validateValue(value, where) {
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) fail(`${where} must be a safe integer or an exact string`);
    return value;
  }
  if (typeof value === 'string' && value.length <= MAX_VALUE_LENGTH && !/[\u0000-\u001f\u007f]/.test(value)) {
    return value;
  }
  fail(`${where} must be a scalar precomputed value`);
}

function validateFact(fact, index) {
  const where = `facts[${index}]`;
  if (!isPlainObject(fact)) fail(`${where} must be an object`);
  for (const key of Object.keys(fact)) {
    if (!FACT_KEYS.has(key)) fail(`${where}.${key} is not supported`);
  }
  for (const key of ['field_ref', 'status', 'reason_code', 'value', 'unit']) {
    if (!Object.prototype.hasOwnProperty.call(fact, key)) fail(`${where}.${key} is required`);
  }
  validateFieldRef(fact.field_ref, `${where}.field_ref`);
  if (typeof fact.status !== 'string' || fact.status.length === 0 || !STATUS.test(fact.status)) {
    fail(`${where}.status is malformed`);
  }
  if (typeof fact.reason_code !== 'string' || !REASON_CODE.test(fact.reason_code)) {
    fail(`${where}.reason_code is malformed`);
  }
  validateValue(fact.value, `${where}.value`);
  if (fact.unit !== null && (typeof fact.unit !== 'string' || fact.unit.length > 64 ||
      !/^[A-Za-z0-9%_./:-]+$/.test(fact.unit))) {
    fail(`${where}.unit is malformed`);
  }

  const hasSourceRef = Object.prototype.hasOwnProperty.call(fact, 'source_ref');
  const hasSourceRefs = Object.prototype.hasOwnProperty.call(fact, 'source_refs');
  if (!hasSourceRef && !hasSourceRefs) fail(`${where}.source_ref(s) is required`);
  if (hasSourceRef) validateSourceRef(fact.source_ref, `${where}.source_ref`);
  if (hasSourceRefs) {
    if (!Array.isArray(fact.source_refs) || fact.source_refs.length < 1 || fact.source_refs.length > 16) {
      fail(`${where}.source_refs must be a non-empty array`);
    }
    fact.source_refs.forEach((ref, sourceIndex) => validateSourceRef(ref, `${where}.source_refs[${sourceIndex}]`));
  }
  if (fact.label !== undefined && (typeof fact.label !== 'string' || fact.label.length > 256)) {
    fail(`${where}.label is malformed`);
  }
  return fact;
}

function assertEnvelopeShape(envelope) {
  if (!isPlainObject(envelope)) fail('AssistantEnvelope must be an object');
  if (Object.prototype.hasOwnProperty.call(envelope, 'portfolio') ||
      Object.prototype.hasOwnProperty.call(envelope, 'positions') ||
      Object.prototype.hasOwnProperty.call(envelope, 'holdings')) {
    fail('raw portfolio data is not an AssistantEnvelope');
  }
  for (const key of Object.keys(envelope)) {
    if (!ENVELOPE_KEYS.has(key)) fail(`envelope.${key} is not supported`);
  }
  if (envelope.schema_version !== ASSISTANT_SCHEMA_VERSION) {
    fail(`schema_version must be ${ASSISTANT_SCHEMA_VERSION}`);
  }
  if (typeof envelope.method_version !== 'string' || !METHOD_VERSION.test(envelope.method_version)) {
    fail('method_version is required and malformed');
  }
  if (!isUtcTimestamp(envelope.as_of)) fail('as_of must be a valid UTC timestamp ending in Z');
  if (!Array.isArray(envelope.facts) || envelope.facts.length < 1 || envelope.facts.length > MAX_FACTS) {
    fail('facts must be a non-empty bounded array');
  }
  const refs = new Set();
  envelope.facts.forEach((fact, index) => {
    validateFact(fact, index);
    if (refs.has(fact.field_ref)) fail(`duplicate field_ref ${fact.field_ref}`);
    refs.add(fact.field_ref);
  });
  const envelopeBytes = new TextEncoder().encode(JSON.stringify(envelope)).byteLength;
  if (envelopeBytes > MAX_ENVELOPE_BYTES) {
    fail(`AssistantEnvelope exceeds the ${MAX_ENVELOPE_BYTES}-byte aggregate limit`);
  }
  return envelope;
}

export function assertValidEnvelope(envelope) {
  return assertEnvelopeShape(envelope);
}

// Result form is convenient for HTTP boundaries; the throwing form above is useful in pure code.
export function validateEnvelope(envelope) {
  try {
    assertEnvelopeShape(envelope);
    return { ok: true, envelope };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : 'invalid AssistantEnvelope' };
  }
}

export const validateAssistantEnvelope = validateEnvelope;

export function selectFacts(envelope, fieldRefs) {
  assertEnvelopeShape(envelope);
  const byRef = new Map(envelope.facts.map((fact) => [fact.field_ref, fact]));
  if (fieldRefs === undefined || fieldRefs === null) return envelope.facts.slice();
  if (!Array.isArray(fieldRefs) || fieldRefs.length < 1 || fieldRefs.length > MAX_FACTS) {
    fail('field_refs must be a bounded non-empty array');
  }
  const selected = [];
  const seen = new Set();
  fieldRefs.forEach((ref, index) => {
    validateFieldRef(ref, `field_refs[${index}]`);
    if (seen.has(ref)) fail(`duplicate field_refs entry ${ref}`);
    seen.add(ref);
    if (!byRef.has(ref)) fail(`unknown field_ref ${ref}`);
    selected.push(byRef.get(ref));
  });
  return selected;
}

export const selectGroundedFacts = selectFacts;

function normalizedNumber(value) {
  const text = String(value).replaceAll(',', '').replace(/%$/, '').trim();
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) return null;
  const number = Number(text);
  if (!Number.isFinite(number)) return null;
  return String(number).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
}

function collectNumbers(value, allowed) {
  const direct = normalizedNumber(value);
  if (direct !== null) allowed.add(direct);
  const parts = String(value).match(/\d[\d,]*(?:\.\d+)?/g) || [];
  for (const part of parts) {
    const number = normalizedNumber(part);
    if (number !== null) allowed.add(number);
  }
}

function allowedNumbers(facts, envelope) {
  const allowed = new Set();
  for (const fact of facts) {
    if (typeof fact.value === 'number' || typeof fact.value === 'string') {
      collectNumbers(fact.value, allowed);
    }
    collectNumbers(fact.field_ref, allowed);
    if (fact.source_ref) collectNumbers(fact.source_ref, allowed);
    for (const sourceRef of fact.source_refs || []) collectNumbers(sourceRef, allowed);
  }
  collectNumbers(envelope.as_of, allowed);
  collectNumbers(envelope.method_version, allowed);
  return allowed;
}

const STATUS_ALIASES = new Map([
  ['BID', ['bid', 'approved', 'accepted', 'passes', 'passed']],
  ['NO BID', ['no bid', 'not bid', 'not approved', 'rejected', 'declined', 'fails', 'failed']],
  ['NO QUOTE', ['no quote', 'unquoted', 'unpriceable', 'cannot price']],
  ['AVAILABLE', ['available']],
  ['UNAVAILABLE', ['unavailable', 'not available']],
  ['SUPPORTED', ['supported']],
  ['UNSUPPORTED', ['unsupported', 'not supported']],
  ['PARTIALLY_SUPPORTED', ['partially supported']],
  ['CONTEXT_ONLY', ['context only']],
  ['UNRESOLVED', ['unresolved']],
  ['ELIGIBLE', ['eligible']],
  ['REJECTED', ['rejected']],
  ['ANALYTICAL_ONLY', ['analytical only']],
  ['NOT_RANKABLE', ['not rankable']],
  ['PASS', ['pass', 'passes', 'passed']],
  ['FAIL', ['fail', 'fails', 'failed']],
  ['UNKNOWN', ['unknown']],
  ['COMPUTED', ['computed']],
  ['NOT_COMPUTABLE', ['not computable']],
  ['INSUFFICIENT_HISTORY', ['insufficient history']],
  ['INSUFFICIENT_COVERAGE', ['insufficient coverage']],
  ['STALE_INPUT', ['stale input']],
  ['MISSING_PRICE', ['missing price']],
  ['SOURCE_CONFLICT', ['source conflict']],
  ['MISSING_LIQUIDITY', ['missing liquidity']],
  ['MISSING_EXECUTION_QUOTE', ['missing execution quote']],
  ['METHODOLOGY_NOT_APPLICABLE', ['methodology not applicable']],
  ['LEGAL_REVIEW_REQUIRED', ['legal review required']],
  ['VELA_UNAVAILABLE', ['vela unavailable']],
]);

function statusPhrases(status) {
  const canonical = status.toUpperCase().replaceAll('_', ' ');
  return STATUS_ALIASES.get(status.toUpperCase()) || [canonical.toLowerCase()];
}

function containsPhrase(text, phrase) {
  const escaped = phrase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(?:^|\\b)${escaped}(?:$|\\b)`, 'i').test(text);
}

function statusFacts(facts) {
  return facts;
}

function requiredStatusFacts(facts) {
  return facts.filter((fact) => /(?:^|[/:.])(?:status|verdict|decision|eligibility)$/i.test(fact.field_ref));
}

function checkStatuses(text, facts) {
  const relevant = statusFacts(facts);
  const required = requiredStatusFacts(facts);
  for (const fact of required) {
    const expected = statusPhrases(fact.status);
    const scope = required.length === 1
      ? text
      : text.split(/\r?\n/).find((line) => line.includes(fact.field_ref));
    if (!scope) return `prose omits the exact field_ref ${fact.field_ref}`;
    if (!expected.some((phrase) => containsPhrase(scope, phrase))) {
      return `prose does not state the ${fact.status} status for ${fact.field_ref}`;
    }
    const expectedCanonical = fact.status.toUpperCase();
    const known = [...STATUS_ALIASES.entries()].flatMap(([canonical, phrases]) =>
      phrases.map((phrase) => ({ canonical, phrase })));
    for (const candidate of known) {
      if (candidate.canonical === expectedCanonical) continue;
      // Aliases overlap ("rejected" is a NO BID synonym, and "bid" is part of
      // "no bid"). An exact or contained expected phrase is not a status change.
      if (expected.some((phrase) => phrase === candidate.phrase || phrase.includes(candidate.phrase))) continue;
      if (containsPhrase(scope, candidate.phrase)) {
        return `prose changes ${fact.field_ref} from ${fact.status} to ${candidate.canonical}`;
      }
    }
  }
  const expectedCanonicals = new Set(relevant.map((fact) => fact.status.toUpperCase()));
  const expectedPhrases = relevant.flatMap((fact) => statusPhrases(fact.status));
  for (const [canonical, phrases] of STATUS_ALIASES.entries()) {
    if (expectedCanonicals.has(canonical)) continue;
    if (phrases.some((phrase) => !expectedPhrases.some((expected) => expected.includes(phrase)) && containsPhrase(text, phrase))) {
      return `prose introduces unavailable status ${canonical}`;
    }
  }
  return null;
}

function checkFieldReferences(text, facts) {
  const allowed = new Set();
  for (const fact of facts) {
    allowed.add(fact.field_ref);
    if (fact.source_ref) allowed.add(fact.source_ref);
    for (const sourceRef of fact.source_refs || []) allowed.add(sourceRef);
  }
  const candidates = text.match(/[a-z][a-z0-9_-]*(?:(?:[/:.])[A-Za-z0-9_-]+|\[[0-9]+\])+/gi) || [];
  for (const candidate of candidates) {
    if (!allowed.has(candidate) && !/^https?:/i.test(candidate)) {
      return `prose references unavailable field_ref ${candidate}`;
    }
  }
  return null;
}

export function checkGrounding(text, envelope, fieldRefs) {
  try {
    const facts = selectFacts(envelope, fieldRefs);
    if (typeof text !== 'string' || text.trim().length === 0) return { ok: false, detail: 'model returned nothing' };
    if (text.length > 4000) return { ok: false, detail: 'explanation exceeds the bounded size' };

    const allowed = allowedNumbers(facts, envelope);
    const numbers = text.match(/\d[\d,]*(?:\.\d+)?/g) || [];
    for (const token of numbers) {
      const normalized = normalizedNumber(token);
      if (normalized !== null && !allowed.has(normalized)) {
        return { ok: false, detail: `invented number ${token}: not present in the facts` };
      }
    }
    const statusError = checkStatuses(text, facts);
    if (statusError) return { ok: false, detail: statusError };
    const refError = checkFieldReferences(text, facts);
    if (refError) return { ok: false, detail: refError };
    return { ok: true, detail: 'grounded: values, statuses, and field references match the envelope' };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : 'grounding failed' };
  }
}

export const groundExplanation = checkGrounding;

export function buildExplanationPrompt(question, envelope, fieldRefs) {
  const facts = selectFacts(envelope, fieldRefs);
  if (typeof question !== 'string' || question.trim().length === 0 || question.length > 2000) {
    fail('question must be a bounded non-empty string');
  }
  return [
    'Explain the supplied deterministic facts in plain language.',
    'Use only the supplied values, units, statuses, reason codes, and source references.',
    'Do not calculate, compare, rank, infer, or add any number. Do not change a status.',
    'For each status field, use a separate line containing its exact field_ref and recorded status.',
    `Question: ${question.trim()}`,
    `Facts: ${JSON.stringify(facts)}`,
  ].join('\n');
}

export function explainEnvelope(args = {}) {
  if (!isPlainObject(args) || Object.keys(args).some((key) => !['envelope', 'field_refs'].includes(key))) {
    fail('explain_envelope accepts only envelope and field_refs');
  }
  const { envelope, field_refs: fieldRefs } = args;
  const facts = selectFacts(envelope, fieldRefs);
  return {
    assistant_status: 'deterministic-ready',
    schema_version: envelope.schema_version,
    method_version: envelope.method_version,
    as_of: envelope.as_of,
    field_refs: facts.map((fact) => fact.field_ref),
    facts,
  };
}

// A single read-only tool. The model can request existing refs; it cannot supply a portfolio or
// an operation that changes state.
export const TOOLS = Object.freeze([{
  type: 'function',
  name: 'explain_envelope',
  description: 'Select existing deterministic facts from a versioned AssistantEnvelope for explanation.',
  parameters: {
    type: 'object',
    additionalProperties: false,
    properties: {
      envelope: {
        type: 'object',
        description: 'A fenorium/assistant-envelope/1 containing precomputed facts only.',
      },
      field_refs: {
        type: 'array',
        items: { type: 'string' },
        description: 'Existing stable field_ref values to explain.',
      },
    },
    required: ['envelope'],
  },
}]);

export const HANDLERS = Object.freeze({ explain_envelope: explainEnvelope });
