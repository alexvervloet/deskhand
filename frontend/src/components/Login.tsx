import { useState } from "react";
import { api, setToken, type User } from "../api";

const DEMO = [
  { email: "owner@northwind.test", note: "can approve" },
  { email: "viewer@northwind.test", note: "cannot approve" },
  { email: "owner@lumen.test", note: "a second merchant" },
];

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [email, setEmail] = useState("owner@northwind.test");
  const [password, setPassword] = useState("demo-password-123");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.login(email, password);
      setToken(result.token);
      onSignedIn(result.user);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h1>Deskhand</h1>
        <p className="lede">
          A support agent that can refund money. Sign in as someone who may approve
          that, or as someone who may only watch.
        </p>

        <label htmlFor="email">Email</label>
        <input
          id="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="username"
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />

        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        {error && <div className="error">{error}</div>}

        <div className="hints">
          Demo accounts, password <code>demo-password-123</code>:
          <div>
            {DEMO.map((account) => (
              <button
                key={account.email}
                type="button"
                onClick={() => setEmail(account.email)}
                title={account.note}
              >
                {account.email.split("@")[0]} — {account.note}
              </button>
            ))}
          </div>
        </div>
      </form>
    </div>
  );
}
