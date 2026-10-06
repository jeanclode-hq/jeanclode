// On <html>, not <body>: a zoom the demo agent applies to <body> leaves the cursor and annotations in place.
(showCursor) => {
  if (window.top !== window || window.__demo) return;
  const host = document.createElement("jeanclode-demo");
  host.style.cssText =
    "position:fixed;inset:0;z-index:2147483647;pointer-events:none;display:block";
  const root = host.attachShadow({ mode: "open" });
  const mount = () => {
    if (!host.isConnected && document.documentElement) document.documentElement.appendChild(host);
    return root;
  };
  window.__demo = { root, mount };
  document.addEventListener("DOMContentLoaded", mount);
  if (!showCursor) return;

  root.innerHTML = `<style>
    .jc-cursor { position: fixed; left: 0; top: 0; width: 24px; height: 24px; opacity: 0;
      filter: drop-shadow(0 1px 2px rgba(0,0,0,.35)); transition: transform 160ms cubic-bezier(.2,.7,.2,1); }
    .jc-ripple { position: fixed; width: 36px; height: 36px; margin: -18px 0 0 -18px; border-radius: 50%;
      border: 2px solid #6366f1; background: rgba(99,102,241,.25); animation: jc-ripple 500ms ease-out forwards; }
    @keyframes jc-ripple { from { transform: scale(.3); opacity: 1 } to { transform: scale(1.4); opacity: 0 } }
  </style>
  <svg class="jc-cursor" viewBox="0 0 24 24"><path d="M4 2 L20 11.5 L13 13 L9.5 20 Z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
  const cursor = root.querySelector(".jc-cursor");
  const moveTo = (x, y) => {
    // The first position snaps instead of gliding in from the corner.
    if (cursor.style.opacity !== "1") {
      cursor.style.transition = "none";
      requestAnimationFrame(() => (cursor.style.transition = ""));
    }
    cursor.style.opacity = "1";
    cursor.style.transform = `translate(${x - 4}px, ${y - 2}px)`;
  };
  const opts = { capture: true, passive: true };
  addEventListener("mousemove", (e) => (mount(), moveTo(e.clientX, e.clientY)), opts);
  addEventListener(
    "mousedown",
    (e) => {
      mount();
      moveTo(e.clientX, e.clientY);
      const ripple = document.createElement("div");
      ripple.className = "jc-ripple";
      ripple.style.left = `${e.clientX}px`;
      ripple.style.top = `${e.clientY}px`;
      root.appendChild(ripple);
      setTimeout(() => ripple.remove(), 600);
    },
    opts,
  );
};
