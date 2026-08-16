/* ---------------------------------------------------------------------------
   landing.js -- FSTrading scroll-scrubbed 3D deck.

   Dependency-free by necessity: FastAPI serves this directory as static files
   with no build step. No GSAP, no ScrollTrigger, no bundler, no React.

   WHY SCRUBBED, NOT TRIGGERED
   ---------------------------
   The previous version swapped a class at a scroll threshold and let a CSS
   transition play on its own clock. That reads as "nothing, nothing, POP" --
   the motion is disconnected from the wheel. Here, scroll position drives the
   transform directly: every frame each panel gets a signed distance from the
   viewer and CSS maps that straight onto translateZ. Scroll a little, things
   move a little. Stop mid-flight and they hang in mid-air. Scroll backwards
   and they retreat. There is no transition to wait for.

   PER PANEL, EVERY FRAME
     --d     signed distance from the camera in panel units.
             -1 = one panel back in the distance, 0 = at the camera plane,
             +1 = one panel past you and behind your head.
     --o     opacity, faded out as |--d| grows
     --blur  depth-of-field blur in px

   CSS multiplies --d by a per-element --depth, so a heading and a stage card
   at different depths separate as they travel. That parallax is what makes it
   read as a space rather than a sliding sheet.

   ACCESSIBILITY
   -------------
   Reduced motion or no JS: the deck un-pins into ordinary stacked sections
   via CSS and this file marks every panel visible. That path is load-bearing
   -- panels are absolutely stacked, so a dead engine would otherwise leave
   panel one on screen and the entire rest of the site invisible.
   --------------------------------------------------------------------------- */

(function () {
  "use strict";

  var root = document.documentElement;
  root.classList.add("js");

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var deck = document.querySelector("[data-deck]");
  var panels = deck ? [].slice.call(deck.querySelectorAll("[data-panel]")) : [];
  var reveals = [].slice.call(document.querySelectorAll("[data-reveal]"));
  var rail = document.querySelector("[data-rail]");
  var railDots = [];

  function clamp(n, lo, hi) { return n < lo ? lo : n > hi ? hi : n; }

  /* ---- degraded modes: show everything, bind nothing ------------------ */

  if (reduce) {
    reveals.forEach(function (el) { el.classList.add("is-in"); });
    panels.forEach(function (p) { p.classList.add("is-on"); });
    return;
  }

  /* ---- real viewport height (2026-08-16, mobile fly-through) ----------
     100vh is unreliable on mobile: iOS Safari's address bar collapses and
     expands as you scroll, changing 100vh mid-scroll and fighting both
     position:sticky pinning and anything sized off vh. --vh is set from
     window.innerHeight instead, and kept live via resize/orientationchange
     plus the Visual Viewport API's own resize event where available --
     that one fires more reliably than window's for the toolbar show/hide
     specifically, which is the case that matters here. landing.css uses
     calc(var(--vh, 1vh) * 100) wherever it used to assume a stable 100vh. */
  function setVh() {
    root.style.setProperty("--vh", window.innerHeight * 0.01 + "px");
  }
  setVh();
  window.addEventListener("resize", setVh);
  window.addEventListener("orientationchange", setVh);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", setVh);
  }

  if (deck) deck.style.setProperty("--panels", panels.length);

  // Rail is generated from the panel count so it can't drift out of sync.
  if (rail && panels.length) {
    for (var r = 0; r < panels.length; r++) rail.appendChild(document.createElement("i"));
    railDots = [].slice.call(rail.children);
    if (railDots[0]) railDots[0].classList.add("on");
  }

  /* ---- reveals for the conventional tail below the deck --------------- */

  if ("IntersectionObserver" in window) {
    document.querySelectorAll("[data-stagger]").forEach(function (group) {
      var step = parseInt(group.getAttribute("data-stagger"), 10) || 70;
      [].slice.call(group.querySelectorAll("[data-reveal]")).forEach(function (el, i) {
        el.style.transitionDelay = i * step + "ms";
      });
    });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add("is-in");
        io.unobserve(e.target);
      });
    }, { rootMargin: "0px 0px -12% 0px", threshold: 0.12 });
    reveals.forEach(function (el) { io.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add("is-in"); });
  }

  /* ---- frame loop ------------------------------------------------------ */

  var bar = document.querySelector("[data-progress]");
  var header = document.querySelector("[data-header]");
  var active = -1;
  var ticking = false;

  // ---- motion budget -------------------------------------------------
  // Each panel owns exactly 1.0 of scroll between its full view and the
  // next one's. That budget must cover: this panel holding still, this
  // panel leaving, a clear gap, the next arriving, and the next holding
  // still. The approach window alone used to be 1.25, so a panel began
  // fading in long before the one in front had gone -- the bleed-through.
  //
  //   HOLD  0.15 x2   full view, nothing moving  (the pause)
  //   OUT_W 0.28      leaving
  //   gap   0.06      clear screen
  //   IN_W  0.36      arriving
  //   --------------
  //         1.00      exactly one panel of scroll
  var HOLD  = 0.15;
  var IN_W  = 0.36;
  var OUT_W = 0.28;

  // Travel distances, in the units --depth is multiplied by.
  var IN_MAX_DT  = 0.86;   // arrives from ~0.55x
  var OUT_MAX_DT = 0.52;   // leaves at ~2.0x

  // Past this a panel is fully faded, so it gets visibility:hidden and
  // stops costing anything to composite.
  var RANGE = HOLD + Math.max(IN_W, OUT_W) + 0.02;

  function frame() {
    ticking = false;

    var y = window.pageYOffset || root.scrollTop;
    var vh = window.innerHeight;
    var max = document.body.scrollHeight - vh;
    var pageP = max > 0 ? clamp(y / max, 0, 1) : 0;

    if (bar) bar.style.transform = "scaleX(" + pageP + ")";
    if (header) header.classList.toggle("is-stuck", y > 24);

    // Drifts the fixed backdrop very slightly across the whole page so the
    // background isn't dead still behind moving content.
    root.style.setProperty("--scroll", pageP.toFixed(4));

    if (!deck || !panels.length) return;

    var box = deck.getBoundingClientRect();
    var travel = deck.offsetHeight - vh;
    var dp = travel > 0 ? clamp(-box.top / travel, 0, 1) : (box.top <= 0 ? 1 : 0);

    var n = panels.length;
    // Continuous position along the deck, in panel units.
    var pos = dp * (n - 1);
    var idx = clamp(Math.round(pos), 0, n - 1);

    for (var i = 0; i < n; i++) {
      var el = panels[i];
      var d = pos - i;                 // signed: <0 ahead, 0 here, >0 passed
      var ad = Math.abs(d);

      if (ad > RANGE) {
        if (el.dataset.off !== "1") {
          el.dataset.off = "1";
          el.style.visibility = "hidden";
          el.style.willChange = "auto";
          el.style.setProperty("--o", "0");
        }
        continue;
      }
      el.dataset.off = "0";
      el.style.visibility = "visible";

      // Three states, not two: approaching, HELD at full view, leaving.
      //
      // The hold is a real dead zone -- opacity exactly 1, travel exactly
      // 0 -- so the panel sits perfectly still while you keep scrolling.
      // That beat of stillness is what makes a panel feel arrived at
      // rather than passed through.
      //
      // Approach and departure stay asymmetric: a departing element grows
      // past 1x so it is unmissable, while an arriving one is small and
      // needs a longer, gentler ramp to read at all.
      var e, o, blur, dt;
      if (ad <= HOLD) {
        o = 1; blur = 0; dt = 0;
      } else if (d < 0) {
        e    = Math.min(1, (ad - HOLD) / IN_W);
        o    = clamp(1 - Math.pow(e, 1.4), 0, 1);
        blur = 0;
        dt   = -e * IN_MAX_DT;
      } else {
        e    = Math.min(1, (ad - HOLD) / OUT_W);
        o    = clamp(1 - Math.pow(e, 1.3), 0, 1);
        blur = e * 9;
        dt   = e * OUT_MAX_DT;
      }

      // Perspective division done here rather than in CSS: an element at
      // translateZ(-z) under perspective(P) renders at P/(P+z). Computing the
      // scale directly means no 3D transforms anywhere, which is the whole
      // point -- see landing.css.
      var P = 1100;
      function bandScale(depth) {
        var z = dt * depth;
        var s = P / (P - z);
        return s < 0.25 ? 0.25 : s > 3.2 ? 3.2 : s;
      }

      el.style.setProperty("--d", d.toFixed(4));
      el.style.setProperty("--o", o.toFixed(4));
      el.style.setProperty("--blur", blur.toFixed(2));

      // One scale per depth band. Deeper bands travel further, which is what
      // reads as depth now that there is no actual Z axis.
      el.style.setProperty("--sa", bandScale(1400).toFixed(4));  // stages
      el.style.setProperty("--sb", bandScale(1250).toFixed(4));  // eyebrow
      el.style.setProperty("--sc", bandScale(1050).toFixed(4));  // headline
      el.style.setProperty("--sd", bandScale(880).toFixed(4));   // body
      el.style.setProperty("--se", bandScale(780).toFixed(4));   // chips/lists
      el.style.setProperty("--sf", bandScale(640).toFixed(4));   // buttons

      // Whatever is nearer the viewer paints on top, so a panel flying past
      // correctly occludes the one arriving behind it.
      el.style.zIndex = String(100 + Math.round(d * 10));

      // Only departing panels get a blur filter at all -- see landing.css.
      var leaving = d > 0 ? "1" : "0";
      if (el.dataset.leaving !== leaving) el.dataset.leaving = leaving;

      // Promote ONLY the two or three panels near the camera. Declaring
      // will-change in CSS put ~200 elements in the layer budget at once,
      // past which browsers start dropping paints.
      el.style.willChange = "transform, opacity";
      el.style.pointerEvents = ad < 0.5 ? "auto" : "none";
    }

    if (idx !== active) {
      active = idx;
      for (var k = 0; k < n; k++) panels[k].classList.toggle("is-on", k === idx);
      railDots.forEach(function (dot, m) { dot.classList.toggle("on", m === idx); });
    }
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(frame);
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  frame();

  /* ---- snap navigation -------------------------------------------------
     One gesture = one panel, animated over a fixed duration so the whole
     approach-hold-depart cycle is actually watchable. Native scrolling is
     wheel-speed dependent: a flick would blow through three panels and a
     nudge would leave you stranded mid-transition. Taking over the wheel is
     the only way to guarantee every visitor sees the same motion.

     The frame loop is untouched -- these handlers only move the scroll
     position, and the fly-through still reads that position every frame. So
     the animation stays scrubbed; it is just being scrubbed by us.

     Only active where the deck is pinned. On mobile and under reduced
     motion the page scrolls normally, which is what those users expect.
     -------------------------------------------------------------------- */

  var snapOK = window.matchMedia(
    "(min-width: 901px) and (prefers-reduced-motion: no-preference)").matches;

  // One panel per gesture. This is the single number that controls how
  // fast the site feels -- raise it to linger, lower it to tighten up.
  var STEP_MS = 3000;   // ~3s to cross a panel: arrive, hold, depart
  var COOLDOWN = 260;   // swallow trackpad momentum after a step lands

  var animating = false;
  var lockUntil = 0;

  function panelY(i) {
    var travel = deck.offsetHeight - window.innerHeight;
    return deck.offsetTop + (travel * i) / (panels.length - 1);
  }

  function currentIndex() {
    var travel = deck.offsetHeight - window.innerHeight;
    var dp = travel > 0
      ? clamp(-deck.getBoundingClientRect().top / travel, 0, 1) : 0;
    return Math.round(dp * (panels.length - 1));
  }

  // Ease in and out: starts gently, coasts, settles. A linear scroll reads
  // as mechanical and makes the hold at each end hard to feel.
  function ease(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function goTo(i, ms) {
    i = clamp(i, 0, panels.length - 1);
    var from = window.pageYOffset || root.scrollTop;
    var to = panelY(i);
    var dist = to - from;
    if (Math.abs(dist) < 2) return;
    var t0 = performance.now();
    animating = true;
    (function step(now) {
      var t = Math.min(1, (now - t0) / ms);
      window.scrollTo(0, from + dist * ease(t));
      if (t < 1) { requestAnimationFrame(step); }
      else { animating = false; lockUntil = performance.now() + COOLDOWN; }
    })(t0);
  }

  // The deck only owns the wheel while it actually fills the viewport.
  // Outside that, the tail below it scrolls normally.
  function deckEngaged() {
    var r = deck.getBoundingClientRect();
    return r.top <= 1 && r.bottom >= window.innerHeight - 1;
  }

  function nav(dir) {
    if (animating || performance.now() < lockUntil) return;
    goTo(currentIndex() + dir, STEP_MS);
  }

  // A panel whose content is still taller than the screen despite the
  // compact-mode CSS rules falls back to its own internal scroll (see
  // .panel-inner's overflow-y:auto in landing.css). Both interception
  // handlers below must let that internal scroll run to its own boundary
  // before taking the gesture over for deck navigation -- otherwise
  // preventDefault() blocks the nested scroll along with everything else
  // and the tail of an overflowing panel becomes permanently unreachable.
  function atScrollBoundary(dir) {
    var el = panels[currentIndex()].querySelector(".panel-inner");
    if (!el || el.scrollHeight <= el.clientHeight + 1) return true;
    return dir > 0
      ? el.scrollTop + el.clientHeight >= el.scrollHeight - 1
      : el.scrollTop <= 1;
  }

  if (snapOK && deck && panels.length > 1) {
    window.addEventListener("wheel", function (e) {
      if (e.ctrlKey) return;                  // pinch-zoom
      if (!deckEngaged()) return;             // tail scrolls natively
      var dir = e.deltaY > 0 ? 1 : -1;
      var idx = currentIndex();
      var r = deck.getBoundingClientRect();
      // Hand control back at the ends so the page can leave the deck.
      if (dir > 0 && idx >= panels.length - 1 && r.bottom <= window.innerHeight + 1) return;
      if (dir < 0 && idx <= 0 && r.top >= -1) return;
      if (!atScrollBoundary(dir)) return;     // let panel-inner's own scroll consume this first
      e.preventDefault();
      if (Math.abs(e.deltaY) < 4) return;
      nav(dir);
    }, { passive: false });

    window.addEventListener("keydown", function (e) {
      if (!deckEngaged()) return;
      var k = e.key;
      if (k === "ArrowDown" || k === "PageDown" || k === " ") { e.preventDefault(); nav(1); }
      else if (k === "ArrowUp" || k === "PageUp") { e.preventDefault(); nav(-1); }
      else if (k === "Home") { e.preventDefault(); goTo(0, 3400); }
    });
  }

  /* ---- touch swipe navigation (2026-08-16, one-swipe-one-panel) --------
     Mirrors the wheel-interception block above, driven by touch deltas
     instead of wheel deltaY: wheel events never fire for touch scrolling
     at all, so without this, a flick's native momentum could carry the
     scroll position through several panels before the settle-snap below
     ever got a chance to intervene -- landing on whichever panel was
     nearest wherever momentum ran out, not necessarily the next one.

     Bound unconditionally, not gated to snapOK's min-width:901px check:
     touch events simply don't fire from non-touch input regardless of
     viewport width, so a touch-capable laptop correctly gets wheel-nav
     from its trackpad AND swipe-nav from its touchscreen, not one or the
     other. Reuses nav()/currentIndex()/deckEngaged() unchanged. */
  if (deck && panels.length > 1) {
    var touchStartX = 0, touchStartY = 0, touchDecided = false, touchNavigated = false;
    var TOUCH_THRESHOLD = 10; // px of drag before treating this as a deliberate swipe

    window.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1) return; // ignore pinch/multi-touch entirely
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      touchDecided = false;
      touchNavigated = false;
    }, { passive: true });

    window.addEventListener("touchmove", function (e) {
      if (touchNavigated || e.touches.length !== 1) return;
      var dx = e.touches[0].clientX - touchStartX;
      var dy = e.touches[0].clientY - touchStartY;

      if (!touchDecided) {
        if (Math.abs(dx) < TOUCH_THRESHOLD && Math.abs(dy) < TOUCH_THRESHOLD) return;
        touchDecided = true;
        // Horizontal drag: leave it alone entirely (a chip row, a native
        // OS edge-swipe, whatever it is -- not this gesture's business).
        if (Math.abs(dx) > Math.abs(dy)) return;
      }
      if (!deckEngaged()) return; // tail scrolls natively below the deck

      var dir = dy < 0 ? 1 : -1;
      var idx = currentIndex();
      var r = deck.getBoundingClientRect();
      // Same hand-back-control-at-the-ends rule as wheel.
      if (dir > 0 && idx >= panels.length - 1 && r.bottom <= window.innerHeight + 1) return;
      if (dir < 0 && idx <= 0 && r.top >= -1) return;
      // Let an overflowing panel's own scroll run first -- same reasoning
      // as the wheel handler above. Native touch scroll continues on this
      // same drag, so once the content bottoms out the very same swipe
      // naturally carries into deck navigation instead of needing a second one.
      if (!atScrollBoundary(dir)) return;

      e.preventDefault();     // suppress native momentum for the rest of this gesture
      touchNavigated = true;  // commit once -- further drag in the same touch advances no further
      nav(dir);
    }, { passive: false });
  }

  /* ---- scroll-settle snap (2026-08-16, mobile fly-through) -------------
     Wheel-interception above guarantees every desktop gesture lands on a
     whole panel; touch scrolling has no wheel to intercept, so without
     this a swipe could leave the page stopped mid-fade -- a panel half
     transparent and blurred, reading as broken rather than as the effect.
     Reuses goTo/currentIndex/deckEngaged unchanged; only runs where
     snapOK's wheel handling isn't already covering this. */
  if (!snapOK && deck && panels.length > 1) {
    var settleTimer = null;
    var SETTLE_DELAY = 120;  // ms of scroll silence before settling
    var SETTLE_MS = 420;     // quick -- this is a nudge into place, not a tour

    function scheduleSettle() {
      // goTo()'s own scrollTo calls fire scroll events too; skip re-arming
      // while it's already running so the two never fight over the timer.
      if (animating) return;
      clearTimeout(settleTimer);
      settleTimer = setTimeout(function () {
        if (animating || !deckEngaged()) return;
        goTo(currentIndex(), SETTLE_MS);
      }, SETTLE_DELAY);
    }

    window.addEventListener("scroll", scheduleSettle, { passive: true });
  }

  /* ---- rail + in-page nav --------------------------------------------- */

  railDots.forEach(function (dot, i) {
    dot.addEventListener("click", function () {
      if (!deck) return;
      if (!snapOK) {
        var travel = deck.offsetHeight - window.innerHeight;
        window.scrollTo({ top: deck.offsetTop + (travel * i) / (panels.length - 1), behavior: "smooth" });
        return;
      }
      // Longer trip for a longer jump, so a rail click across the deck
      // doesn't rocket past fifteen panels in under a second.
      var hops = Math.abs(i - currentIndex());
      goTo(i, Math.min(6000, 1800 + hops * 320));
    });
  });

  document.querySelectorAll('a[href^="#"]').forEach(function (a) {
    a.addEventListener("click", function (e) {
      var t = document.querySelector(a.getAttribute("href"));
      if (!t) return;
      e.preventDefault();
      t.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
})();
