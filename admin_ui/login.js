document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const box = document.getElementById("msg");
  box.classList.add("hidden");
  try {
    const r = await fetch("/admin-api/login", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Admin-Panel": "1"},
      body: JSON.stringify({password: document.getElementById("pw").value})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Login failed");
    location.href = "/admin/dashboard.html";
  } catch (err) {
    box.textContent = err.message;
    box.classList.remove("hidden");
    document.getElementById("pw").value = "";
  }
});

