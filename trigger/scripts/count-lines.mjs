// Reproduce every line count in docs/TRIGGER-PORT.md.
//
//     node scripts/count-lines.mjs
//
// A writeup whose whole stance is careful accounting should not ask anyone to
// take its arithmetic on faith. The first version quoted a total without saying
// which files it covered or how a line was counted, which made it the one claim
// in the document a reader could not check. This script is the method.
//
// A counted line is non-blank, is not a comment-only line, and is not inside a
// Python docstring or a JSDoc/block comment. Trailing comments on a line of
// code still count as code, because the code is there.

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')

// The Python runtime the port replaces, and the TypeScript that replaced it.
// Both lists are the whole of the comparison: no file is in one set whose
// counterpart is missing from the other, and nothing else is counted.
const PYTHON = [
  'deskhand/runtime/loop.py',
  'deskhand/runtime/runs.py',
  'deskhand/runtime/approvals.py',
  'deskhand/runtime/transcript.py',
  'deskhand/worker.py',
  'deskhand/tools/invoke.py',
  'deskhand/tools/base.py',
]

const TYPESCRIPT = [
  'trigger/src/loop.ts',
  'trigger/src/runs.ts',
  'trigger/src/consent.ts',
  'trigger/src/fence.ts',
  'trigger/src/bounds.ts',
  'trigger/src/invoke.ts',
  'trigger/src/tools/registry.ts',
  'trigger/src/trigger/work-ticket.ts',
]

// The mechanisms whose only job was surviving not being in memory. Counted by
// name out of the Python, because "204 lines" is the headline and a reader
// should be able to see which 204.
const DELETED = [
  ['deskhand/runtime/runs.py', 'claim_next'],
  ['deskhand/runtime/runs.py', 'renew_lease'],
  ['deskhand/runtime/runs.py', 'suspend_for_approval'],
  ['deskhand/runtime/runs.py', 'requeue'],
  ['deskhand/runtime/approvals.py', 'expire_stale'],
  ['deskhand/runtime/loop.py', 'LeaseLost'],
  ['deskhand/runtime/loop.py', '_last_model_call'],
  ['deskhand/runtime/loop.py', '_unresolved'],
  ['deskhand/runtime/transcript.py', 'rebuild'],
  ['deskhand/worker.py', '*'],
]

function stripPython(src) {
  return src.replace(/("""|''')[\s\S]*?\1/g, '')
}

function stripTs(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '')
}

function count(src, kind) {
  const stripped = kind === 'py' ? stripPython(src) : stripTs(src)
  return stripped
    .split('\n')
    .filter((line) => {
      const s = line.trim()
      if (!s) return false
      return kind === 'py' ? !s.startsWith('#') : !s.startsWith('//')
    }).length
}

function read(rel) {
  return fs.readFileSync(path.join(repo, rel), 'utf-8')
}

// Pull one top-level def/class out of a Python file by name. Deliberately
// crude: these files are flat, and a real parser would be a dependency for a
// script whose job is to be checkable by eye.
//
// The end of the member is the next line that begins a new top-level statement.
// Testing that with /^\S/ is wrong and quietly undercounts: a wrapped signature
// puts `) -> dict[str, Any] | None:` at column 0, which ended `claim_next` two
// lines in and reported 2 rather than 23. So the end has to look like the start
// of a statement, and it only counts once the body has actually begun.
function pythonMember(src, name) {
  const lines = src.split('\n')
  const start = lines.findIndex((l) => new RegExp(`^(def|class) ${name}\\b`).test(l))
  if (start === -1) throw new Error(`no top-level ${name}`)

  let end = lines.length
  let inBody = false
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i]
    if (!line.trim()) continue
    if (/^\s/.test(line)) {
      inBody = true
      continue
    }
    if (inBody && /^[A-Za-z_@]/.test(line)) {
      end = i
      break
    }
  }
  return lines.slice(start, end).join('\n')
}

function table(rows, headers) {
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map((r) => String(r[i]).length)),
  )
  const line = (cells) =>
    cells.map((c, i) => (i === 1 ? String(c).padStart(widths[i]) : String(c).padEnd(widths[i]))).join('  ')
  console.log(line(headers))
  console.log(widths.map((w) => '-'.repeat(w)).join('  '))
  for (const r of rows) console.log(line(r))
}

let total = 0
const deletedRows = DELETED.map(([file, member]) => {
  const src = read(file)
  const n = member === '*' ? count(src, 'py') : count(pythonMember(src, member), 'py')
  total += n
  return [member === '*' ? path.basename(file) : `${path.basename(file, '.py')}.${member}`, n]
})

console.log('Mechanisms deleted, whose only job was surviving not being in memory\n')
table(deletedRows, ['mechanism', 'lines'])
console.log(`\n  total deleted: ${total}\n`)

const pyRows = PYTHON.map((f) => [f.replace('deskhand/', ''), count(read(f), 'py')])
const tsRows = TYPESCRIPT.map((f) => [f.replace('trigger/src/', ''), count(read(f), 'ts')])
const pyTotal = pyRows.reduce((a, r) => a + r[1], 0)
const tsTotal = tsRows.reduce((a, r) => a + r[1], 0)

console.log('The runtime the port replaces\n')
table(pyRows, ['python', 'lines'])
console.log(`\n  total: ${pyTotal}\n`)

console.log('The TypeScript that replaced it\n')
table(tsRows, ['typescript', 'lines'])
console.log(`\n  total: ${tsTotal}\n`)

console.log(`Python ${pyTotal} -> TypeScript ${tsTotal} (${tsTotal - pyTotal >= 0 ? '+' : ''}${tsTotal - pyTotal})`)
