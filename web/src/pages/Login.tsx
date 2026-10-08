import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Eye, EyeOff, Lock, Mail, ShieldCheck, ArrowRight } from "lucide-react";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/api";
import { Alert } from "../components/ui";
import { LoginHero } from "../components/LoginHero";

// Login: split screen -- form in a glass card on the left, illustrated showcase
// on the right (large screens only; on mobile the form takes the whole screen,
// without distractions). Error messages: one generic message for invalid
// credentials and another for any other error, never distinguishing field or
// cause (same doctrine as auth.py).
export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(email, password);
      navigate("/");
    } catch (err) {
      if (!(err instanceof ApiError)) setError("No se pudo conectar con el servidor");
      else if (err.status === 401) setError("Credenciales inválidas");
      else setError("No se pudo iniciar sesión, intenta de nuevo");
    } finally {
      setLoading(false);
    }
  }

  const fieldWrap =
    "group flex items-center gap-2.5 rounded-2xl border border-line-strong bg-fg/[0.04] px-3.5 transition-[border-color,box-shadow] focus-within:border-brand-500 focus-within:ring-4 focus-within:ring-brand-600/20";

  return (
    <div className="ambient min-h-full">
      <div className="mx-auto grid min-h-screen max-w-[1600px] gap-6 p-4 sm:p-6 lg:grid-cols-[minmax(380px,0.85fr)_minmax(0,1.15fr)] lg:p-6">
        <div className="flex flex-col">
          <header className="flex items-center gap-2.5 px-2 pt-2">
            <span className="glass-card flex h-10 w-10 items-center justify-center rounded-2xl">
              <img src="/logo-mark.png" alt="" className="h-6 w-6 object-contain" />
            </span>
            <span className="text-base font-semibold tracking-tight text-ink">OpenMDVR</span>
          </header>

          <main className="flex flex-1 items-center justify-center py-10">
            <form onSubmit={onSubmit} className="glass-card w-full max-w-[400px] rounded-[28px] p-7 sm:p-8" noValidate={false}>
              <h1 className="text-2xl font-semibold tracking-tight text-ink sm:text-[28px]">Bienvenido</h1>
              <p className="mt-1.5 text-sm text-ink-dim">Inicia sesión para ver tu flota en tiempo real.</p>

              <div className="mt-7 space-y-4">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-ink-dim">Correo electrónico</span>
                  <span className={fieldWrap}>
                    <Mail size={17} strokeWidth={1.75} className="shrink-0 text-ink-faint group-focus-within:text-brand-400" aria-hidden />
                    <input
                      type="email"
                      required
                      autoFocus
                      autoComplete="username"
                      inputMode="email"
                      placeholder="tu@empresa.com"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      className="h-12 min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-ink-faint"
                    />
                  </span>
                </label>

                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-ink-dim">Contraseña</span>
                  <span className={fieldWrap}>
                    <Lock size={17} strokeWidth={1.75} className="shrink-0 text-ink-faint group-focus-within:text-brand-400" aria-hidden />
                    <input
                      type={showPassword ? "text" : "password"}
                      required
                      autoComplete="current-password"
                      placeholder="••••••••"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="h-12 min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-ink-faint"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((v) => !v)}
                      aria-label={showPassword ? "Ocultar contraseña" : "Mostrar contraseña"}
                      aria-pressed={showPassword}
                      className="-mr-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-ink-faint hover:bg-fg/[0.06] hover:text-ink"
                    >
                      {showPassword ? <EyeOff size={17} strokeWidth={1.75} /> : <Eye size={17} strokeWidth={1.75} />}
                    </button>
                  </span>
                </label>
              </div>

              {error && (
                <div className="mt-4" role="alert">
                  <Alert>{error}</Alert>
                </div>
              )}

              <button
                type="submit"
                disabled={loading}
                className="group mt-6 flex h-12 w-full items-center justify-center gap-2 rounded-2xl bg-gradient-to-b from-brand-500 to-brand-600 text-[15px] font-semibold text-white shadow-[0_12px_30px_-10px_rgba(3,125,254,0.85),inset_0_1px_0_rgba(255,255,255,0.25)] transition-all hover:brightness-110 active:scale-[0.99] disabled:opacity-60"
              >
                {loading ? (
                  <span className="h-5 w-5 animate-spin rounded-full border-2 border-white/40 border-t-white" aria-label="Entrando" />
                ) : (
                  <>
                    Entrar
                    <ArrowRight size={18} strokeWidth={2} className="transition-transform group-hover:translate-x-0.5" aria-hidden />
                  </>
                )}
              </button>

              <p className="mt-6 flex items-center justify-center gap-1.5 text-[11px] text-ink-faint">
                <ShieldCheck size={14} strokeWidth={1.75} aria-hidden />
                Conexión cifrada
              </p>
            </form>
          </main>

          <footer className="px-2 pb-1 text-center text-[11px] text-ink-faint lg:text-left">© {new Date().getFullYear()} OpenMDVR</footer>
        </div>

        <aside className="hidden lg:block">
          <LoginHero />
        </aside>
      </div>
    </div>
  );
}
