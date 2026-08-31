/**
 * The database, which is the half of deskhand the port does not touch.
 *
 * Same Postgres, same migrations, same seed. Orders, refunds, tickets, the
 * knowledge base and the audit log are all read and written exactly as the
 * Python service reads and writes them — a refund issued by this port is
 * indistinguishable from one issued by `deskhand.worker`, which is what makes
 * the comparison in docs/TRIGGER-PORT.md a fair one. What moved onto the
 * platform is the runtime, not the domain.
 */

import pg from "pg";

const { Pool } = pg;

// Money arrives as `bigint`/`numeric` from Postgres and node-postgres hands
// those back as strings to avoid a silent precision loss. Everything monetary
// here is integer cents or integer micro-dollars and comfortably inside
// Number.MAX_SAFE_INTEGER, so parse them — but parse them deliberately, in one
// place, rather than letting `"4800" > 1000` be a string comparison somewhere.
pg.types.setTypeParser(pg.types.builtins.INT8, (v) => Number.parseInt(v, 10));
pg.types.setTypeParser(pg.types.builtins.NUMERIC, (v) => Number.parseFloat(v));

export const pool = new Pool({
  connectionString:
    process.env.DATABASE_URL ?? "postgresql://deskhand:deskhand@localhost:5437/deskhand",
  max: 8,
});

export type Row = Record<string, any>;

/**
 * Run `fn` inside one transaction, on one connection.
 *
 * The transaction boundary is the whole reason `invoke.ts` can be as short as
 * it is: the tool's effect and the ledger row that remembers it are written in
 * the same transaction, so there is no window in which one exists without the
 * other. See the note in `tools/invoke.ts` about what that assumption costs
 * when the effect is not a row in this database.
 */
export async function transaction<T>(fn: (db: pg.PoolClient) => Promise<T>): Promise<T> {
  const db = await pool.connect();
  try {
    await db.query("begin");
    const result = await fn(db);
    await db.query("commit");
    return result;
  } catch (error) {
    await db.query("rollback").catch(() => {});
    throw error;
  } finally {
    db.release();
  }
}

export async function one(sql: string, params: unknown[] = []): Promise<Row | null> {
  const { rows } = await pool.query(sql, params);
  return rows[0] ?? null;
}

export async function all(sql: string, params: unknown[] = []): Promise<Row[]> {
  const { rows } = await pool.query(sql, params);
  return rows;
}
