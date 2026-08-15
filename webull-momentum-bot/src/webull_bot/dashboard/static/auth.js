// Signup/login form handlers for /signup and /login -- separate from
// app.js (the authenticated dashboard's ~900-line polling script) since
// neither page needs any of that. Redirects to /app on success. There is
// no self-serve "forgot password" flow (see auth/routes.py's docstring
// for why) -- a failed login just shows the server's error message.

async function submitAuthForm(path, body, resultEl, redirectTo) {
  resultEl.textContent = "";
  resultEl.className = "status-bar";
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      resultEl.textContent = data.detail || `Request failed (${res.status}).`;
      resultEl.classList.add("auth-error");
      return;
    }
    window.location.href = redirectTo;
  } catch (err) {
    resultEl.textContent = "Network error -- please try again.";
    resultEl.classList.add("auth-error");
  }
}

const signupForm = document.getElementById("signup-form");
if (signupForm) {
  signupForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const email = document.getElementById("signup-email").value;
    const password = document.getElementById("signup-password").value;
    submitAuthForm("/api/auth/signup", { email, password }, document.getElementById("signup-result"), "/app");
  });
}

const loginForm = document.getElementById("login-form");
if (loginForm) {
  loginForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const email = document.getElementById("login-email").value;
    const password = document.getElementById("login-password").value;
    submitAuthForm("/api/auth/login", { email, password }, document.getElementById("login-result"), "/app");
  });
}
