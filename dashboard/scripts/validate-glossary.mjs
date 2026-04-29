#!/usr/bin/env node
/**
 * Validates dashboard/src/data/glossary.json against the schema rules:
 *   - every entry has id, term, category, short, long, related
 *   - all ids are unique
 *   - category is one of the allowed values
 *   - every related id resolves to a real entry
 *   - short field is <= 140 chars
 *
 * Usage:  node dashboard/scripts/validate-glossary.mjs
 * Exits non-zero on any violation; safe to wire into CI later.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const GLOSSARY_PATH = resolve(__dirname, '..', 'src', 'data', 'glossary.json')

const ALLOWED_CATEGORIES = new Set(['verdict', 'agent', 'tier', 'score', 'playbook', 'concept'])
const REQUIRED_FIELDS = ['id', 'term', 'category', 'short', 'long', 'related']
const MAX_SHORT_LEN = 140

function fail(msg) {
  console.error(`✗ ${msg}`)
  process.exitCode = 1
}

function ok(msg) {
  console.log(`✓ ${msg}`)
}

let raw
try {
  raw = readFileSync(GLOSSARY_PATH, 'utf8')
} catch (err) {
  fail(`Could not read ${GLOSSARY_PATH}: ${err.message}`)
  process.exit(1)
}

let entries
try {
  entries = JSON.parse(raw)
} catch (err) {
  fail(`Invalid JSON: ${err.message}`)
  process.exit(1)
}

if (!Array.isArray(entries)) {
  fail('glossary.json must be a top-level JSON array')
  process.exit(1)
}

const ids = new Set()
const idCounts = new Map()
let violations = 0

for (const [idx, entry] of entries.entries()) {
  const where = `entry[${idx}]${entry?.id ? ` (id="${entry.id}")` : ''}`

  for (const field of REQUIRED_FIELDS) {
    if (!(field in entry)) {
      fail(`${where}: missing required field "${field}"`)
      violations++
    }
  }

  if (typeof entry.id === 'string') {
    idCounts.set(entry.id, (idCounts.get(entry.id) || 0) + 1)
    ids.add(entry.id)
  }

  if (entry.category && !ALLOWED_CATEGORIES.has(entry.category)) {
    fail(`${where}: category "${entry.category}" not in ${[...ALLOWED_CATEGORIES].join(', ')}`)
    violations++
  }

  if (typeof entry.short === 'string' && entry.short.length > MAX_SHORT_LEN) {
    fail(`${where}: short field is ${entry.short.length} chars (limit ${MAX_SHORT_LEN})`)
    violations++
  }

  if (!Array.isArray(entry.related)) {
    fail(`${where}: "related" must be an array`)
    violations++
  }
}

for (const [id, count] of idCounts) {
  if (count > 1) {
    fail(`duplicate id "${id}" appears ${count} times`)
    violations++
  }
}

for (const entry of entries) {
  if (!Array.isArray(entry?.related)) continue
  for (const relatedId of entry.related) {
    if (!ids.has(relatedId)) {
      fail(`entry "${entry.id}" has related id "${relatedId}" that does not resolve`)
      violations++
    }
  }
}

if (violations === 0) {
  ok(`glossary.json valid — ${entries.length} entries, all schema checks passed`)
}
