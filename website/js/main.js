// Full Stack Growth Studio — scroll motion interactions
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var hasGSAP = typeof gsap !== 'undefined';

  if (hasGSAP) {
    gsap.registerPlugin(ScrollTrigger);
  }

  /* ---------------- Smooth scroll (Lenis) ---------------- */
  var lenis = null;
  if (!reduceMotion && typeof Lenis !== 'undefined') {
    lenis = new Lenis({
      duration: 1.1,
      easing: function (t) { return Math.min(1, 1.001 - Math.pow(2, -10 * t)); },
      smoothWheel: true,
    });
    function raf(time) {
      lenis.raf(time);
      requestAnimationFrame(raf);
    }
    requestAnimationFrame(raf);
    if (hasGSAP) {
      lenis.on('scroll', ScrollTrigger.update);
      gsap.ticker.add(function (time) { lenis.raf(time * 1000); });
      gsap.ticker.lagSmoothing(0);
    }
  }

  /* ---------------- Nav scroll state + burger ---------------- */
  var nav = document.getElementById('nav');
  var burger = document.getElementById('burger');
  var navMobile = document.getElementById('navMobile');

  function onScrollNav() {
    if (window.scrollY > 40) nav.classList.add('is-scrolled');
    else nav.classList.remove('is-scrolled');
  }
  window.addEventListener('scroll', onScrollNav, { passive: true });
  onScrollNav();

  if (burger) {
    burger.addEventListener('click', function () {
      navMobile.classList.toggle('is-open');
      burger.classList.toggle('is-open');
    });
    navMobile.querySelectorAll('a').forEach(function (a) {
      a.addEventListener('click', function () {
        navMobile.classList.remove('is-open');
      });
    });
  }

  /* ---------------- Smooth anchor scrolling ---------------- */
  document.querySelectorAll('a[href^="#"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      var id = a.getAttribute('href');
      if (id.length < 2) return;
      var target = document.querySelector(id);
      if (!target) return;
      e.preventDefault();
      if (lenis) lenis.scrollTo(target, { offset: -70 });
      else target.scrollIntoView({ behavior: 'smooth' });
    });
  });

  /* ---------------- Top progress bar ---------------- */
  var progressBar = document.getElementById('progressBar');
  function updateProgress() {
    var h = document.documentElement;
    var scrollTop = h.scrollTop || document.body.scrollTop;
    var scrollHeight = (h.scrollHeight || document.body.scrollHeight) - h.clientHeight;
    var pct = scrollHeight > 0 ? scrollTop / scrollHeight : 0;
    progressBar.style.transform = 'scaleX(' + pct + ')';
  }
  window.addEventListener('scroll', updateProgress, { passive: true });
  updateProgress();

  /* ---------------- Cursor glow ---------------- */
  var cursorGlow = document.getElementById('cursorGlow');
  if (cursorGlow && window.matchMedia('(hover:hover) and (pointer:fine)').matches) {
    window.addEventListener('mousemove', function (e) {
      cursorGlow.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px) translate(-50%,-50%)';
    });
  }

  /* ---------------- Magnetic buttons ---------------- */
  if (!reduceMotion) {
    document.querySelectorAll('.magnetic').forEach(function (btn) {
      btn.addEventListener('mousemove', function (e) {
        var r = btn.getBoundingClientRect();
        var x = e.clientX - r.left - r.width / 2;
        var y = e.clientY - r.top - r.height / 2;
        btn.style.transform = 'translate(' + x * 0.25 + 'px,' + y * 0.35 + 'px)';
      });
      btn.addEventListener('mouseleave', function () {
        btn.style.transform = 'translate(0,0)';
      });
    });
  }

  /* ---------------- Service card mouse-tracked glow ---------------- */
  document.querySelectorAll('.service-card').forEach(function (card) {
    card.addEventListener('mousemove', function (e) {
      var r = card.getBoundingClientRect();
      card.style.setProperty('--mx', ((e.clientX - r.left) / r.width) * 100 + '%');
      card.style.setProperty('--my', ((e.clientY - r.top) / r.height) * 100 + '%');
    });
  });

  /* ---------------- FAQ accordion ---------------- */
  document.querySelectorAll('.faq__item').forEach(function (item) {
    var q = item.querySelector('.faq__question');
    var a = item.querySelector('.faq__answer');
    q.addEventListener('click', function () {
      var isOpen = item.classList.contains('is-open');
      document.querySelectorAll('.faq__item.is-open').forEach(function (openItem) {
        if (openItem !== item) {
          openItem.classList.remove('is-open');
          openItem.querySelector('.faq__answer').style.maxHeight = null;
        }
      });
      if (isOpen) {
        item.classList.remove('is-open');
        a.style.maxHeight = null;
      } else {
        item.classList.add('is-open');
        a.style.maxHeight = a.scrollHeight + 'px';
      }
    });
  });

  /* ---------------- CTA form (demo — no backend) ---------------- */
  var ctaForm = document.getElementById('ctaForm');
  if (ctaForm) {
    ctaForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var btn = ctaForm.querySelector('button');
      var original = btn.innerHTML;
      btn.innerHTML = '<span>Thanks — we\'ll be in touch!</span>';
      ctaForm.reset();
      setTimeout(function () { btn.innerHTML = original; }, 3200);
    });
  }

  /* ---------------- GSAP scroll animations ----------------
     NOTE: GSAP/ScrollTrigger measures each trigger's start/end against the
     document layout that exists at the moment it's created. A pin:true
     trigger inserts a spacer that pushes everything below it further down
     the page, so every pinned section MUST be created in the same order it
     appears in the DOM — otherwise later triggers get measured before an
     earlier pin's spacer exists and end up with stale (too-small) offsets,
     causing pinned sections to visually overlap. Generic, non-pinning
     scrub reveals are created last, once every pin's spacer is in place. */
  if (hasGSAP && !reduceMotion) {

    var isDesktop = window.innerWidth > 900;

    // Hero title lines
    gsap.set('.reveal-line span', { y: '110%' });
    gsap.to('.reveal-line span', {
      y: 0, duration: 1, ease: 'power4.out', stagger: 0.12, delay: 0.2,
    });

    // Hero content animates on load (not scroll-triggered — it's above the fold)
    document.querySelectorAll('.hero .reveal-up').forEach(function (el) {
      var delay = parseFloat(el.getAttribute('data-delay') || '0');
      gsap.to(el, { opacity: 1, y: 0, duration: 0.9, ease: 'power3.out', delay: delay + 0.4 });
    });

    /* -------- Aurora: whole-page hue drift + parallax blobs -------- */
    gsap.to('#aurora', {
      filter: 'hue-rotate(75deg)', ease: 'none',
      scrollTrigger: { trigger: document.body, start: 'top top', end: 'bottom bottom', scrub: 0.8 },
    });
    gsap.to('.aurora__blob--1', { y: '30vh', x: '5vw', ease: 'none', scrollTrigger: { trigger: document.body, start: 'top top', end: 'bottom bottom', scrub: 1.2 } });
    gsap.to('.aurora__blob--2', { y: '-22vh', x: '-8vw', ease: 'none', scrollTrigger: { trigger: document.body, start: 'top top', end: 'bottom bottom', scrub: 1.5 } });
    gsap.to('.aurora__blob--3', { y: '18vh', x: '-4vw', ease: 'none', scrollTrigger: { trigger: document.body, start: 'top top', end: 'bottom bottom', scrub: 1 } });

    /* -------- Hero: pinned zoom/dissolve exit -------- */
    var heroEl = document.querySelector('.hero');
    if (heroEl) {
      ScrollTrigger.create({
        trigger: heroEl, start: 'top top', end: '+=90%', pin: true, scrub: 0.6, anticipatePin: 1,
        onUpdate: function (self) {
          var p = self.progress;
          gsap.set('.hero__content', { scale: 1 - p * 0.16, opacity: 1 - p * 1.15, filter: 'blur(' + (p * 9) + 'px)' });
          gsap.set('.hero__bg', { scale: 1 + p * 0.3, opacity: 1 - p * 0.7 });
          gsap.set('.scroll-cue', { opacity: 1 - p * 3 });
        },
      });

      // Subtle mouse parallax for orbs (independent of scroll pin)
      heroEl.addEventListener('mousemove', function (e) {
        var r = heroEl.getBoundingClientRect();
        var px = (e.clientX - r.left) / r.width - 0.5;
        var py = (e.clientY - r.top) / r.height - 0.5;
        gsap.to('.orb--cyan', { x: px * 50, y: py * 30, duration: 1.2, ease: 'power2.out', overwrite: 'auto' });
        gsap.to('.orb--purple', { x: -px * 60, y: -py * 30, duration: 1.2, ease: 'power2.out', overwrite: 'auto' });
      });
    }

    // Flow lines gentle drift
    gsap.to('.flow-line', { x: 30, duration: 6, ease: 'sine.inOut', yoyo: true, repeat: -1, stagger: 0.4 });

    /* -------- Zoom word: pinned giant type moment -------- */
    var zoomWordSection = document.getElementById('zoomWord');
    if (zoomWordSection) {
      gsap.set('.zoom-word__bg', { scale: 0.55 });
      ScrollTrigger.create({
        trigger: zoomWordSection, start: 'top top', end: '+=140%', pin: true, scrub: 0.6, anticipatePin: 1,
        onUpdate: function (self) {
          var p = self.progress;
          gsap.set('.zoom-word__bg', { scale: 0.55 + p * 2.1, opacity: 0.5 - Math.abs(p - 0.5) * 0.5 });
          var fgOpacity = p < 0.15 ? p / 0.15 : (p > 0.8 ? Math.max(0, (1 - p) / 0.2) : 1);
          gsap.set('.zoom-word__fg', { opacity: fgOpacity, y: (1 - fgOpacity) * 24 });
        },
      });
    }

    /* -------- Problem cards: 3D scrubbed entrance (comes before Pillars in the DOM) -------- */
    document.querySelectorAll('.card-3d').forEach(function (card) {
      gsap.fromTo(card,
        { opacity: 0, y: 90, rotateX: 38, scale: 0.92 },
        {
          opacity: 1, y: 0, rotateX: 0, scale: 1, ease: 'none',
          scrollTrigger: { trigger: card, start: 'top 92%', end: 'top 55%', scrub: 0.6 },
        }
      );
    });

    /* -------- Pillars pinned scroll section (must be created before Services — it's earlier in the DOM) -------- */
    var pillarSection = document.getElementById('pillars');
    var panels = gsap.utils.toArray('.pillar-panel');
    var items = gsap.utils.toArray('.pillar-item');

    function setActivePillar(i) {
      panels.forEach(function (p, idx) { p.classList.toggle('is-active', idx === i); });
      items.forEach(function (el, idx) { el.classList.toggle('is-active', idx === i); });
    }
    setActivePillar(0);

    if (pillarSection && isDesktop) {
      var count = items.length;
      ScrollTrigger.create({
        trigger: pillarSection,
        start: 'top top+=90',
        end: '+=' + (count * 500),
        pin: true,
        scrub: 0.5,
        onUpdate: function (self) {
          var idx = Math.min(count - 1, Math.floor(self.progress * count));
          setActivePillar(idx);
        },
      });
    } else {
      // Mobile: simple reveal-based activation as each item scrolls into view
      items.forEach(function (el, idx) {
        ScrollTrigger.create({
          trigger: el, start: 'top center', end: 'bottom center',
          onEnter: function () { setActivePillar(idx); },
          onEnterBack: function () { setActivePillar(idx); },
        });
      });
    }

    /* -------- Services: horizontal scroll-jacked track w/ cover-flow tilt -------- */
    var servicesPin = document.getElementById('servicesPin');
    var servicesTrack = document.getElementById('servicesTrack');
    var servicesProgressFill = document.getElementById('servicesProgressFill');

    if (servicesPin && servicesTrack && isDesktop) {
      var cards = gsap.utils.toArray('.service-card', servicesTrack);

      ScrollTrigger.create({
        trigger: servicesPin,
        start: 'top top+=80',
        end: function () { return '+=' + (servicesTrack.scrollWidth - servicesPin.clientWidth); },
        pin: true,
        scrub: 0.6,
        anticipatePin: 1,
        invalidateOnRefresh: true,
        onUpdate: function (self) {
          var maxX = servicesTrack.scrollWidth - servicesPin.clientWidth;
          var x = -maxX * self.progress;
          gsap.set(servicesTrack, { x: x });
          if (servicesProgressFill) servicesProgressFill.style.width = (self.progress * 100) + '%';

          var vw = window.innerWidth;
          cards.forEach(function (card) {
            var r = card.getBoundingClientRect();
            var center = r.left + r.width / 2;
            var dist = gsap.utils.clamp(-1, 1, (center - vw / 2) / (vw / 2));
            gsap.set(card, {
              rotateY: dist * -14,
              scale: 1 - Math.abs(dist) * 0.1,
              opacity: 1 - Math.abs(dist) * 0.45,
            });
          });
        },
      });
    }

    /* -------- Timeline fill scrub -------- */
    var timelineFill = document.getElementById('timelineFill');
    if (timelineFill) {
      gsap.to(timelineFill, {
        height: '100%', ease: 'none',
        scrollTrigger: {
          trigger: '#timeline', start: 'top 70%', end: 'bottom 70%', scrub: 0.6,
        },
      });
    }

    /* -------- Stat counters -------- */
    document.querySelectorAll('.stat__num').forEach(function (el) {
      var target = parseFloat(el.getAttribute('data-count'));
      var obj = { val: 0 };
      ScrollTrigger.create({
        trigger: el, start: 'top 85%', once: true,
        onEnter: function () {
          gsap.to(obj, {
            val: target, duration: 1.8, ease: 'power2.out',
            onUpdate: function () { el.textContent = Math.round(obj.val); },
          });
        },
      });
    });

    /* -------- Final CTA: glow scrub pop -------- */
    var ctaGlow = document.querySelector('.cta-final__glow');
    if (ctaGlow) {
      gsap.fromTo(ctaGlow,
        { opacity: 0.25, scale: 0.7 },
        {
          opacity: 1, scale: 1.15, ease: 'none',
          scrollTrigger: { trigger: '.cta-final', start: 'top 85%', end: 'top 30%', scrub: 0.8 },
        }
      );
    }

    /* -------- Every remaining .reveal-up, tied continuously to scroll position --------
       Created last so every pin's spacer above it is already in the DOM. */
    document.querySelectorAll('.reveal-up').forEach(function (el) {
      if (el.closest('.hero')) return;
      gsap.fromTo(el,
        { opacity: 0, y: 46, scale: 0.97 },
        {
          opacity: 1, y: 0, scale: 1, ease: 'none',
          scrollTrigger: { trigger: el, start: 'top 96%', end: 'top 58%', scrub: 0.6 },
        }
      );
    });

    ScrollTrigger.refresh();
  } else {
    // No-motion fallback: just show everything, no scroll-jacking
    document.querySelectorAll('.reveal-up, .card-3d').forEach(function (el) {
      el.style.opacity = 1; el.style.transform = 'none'; el.style.filter = 'none';
    });
    document.querySelectorAll('.reveal-line span').forEach(function (el) {
      el.style.transform = 'none';
    });
    document.querySelectorAll('.zoom-word__fg').forEach(function (el) { el.style.opacity = 1; });
    document.querySelectorAll('.zoom-word__bg').forEach(function (el) { el.style.transform = 'scale(1.4)'; });
    document.querySelectorAll('.cta-final__glow').forEach(function (el) { el.style.opacity = 1; });
    var svcPin = document.getElementById('servicesPin');
    if (svcPin) svcPin.style.overflowX = 'auto';
    document.querySelectorAll('.pillar-panel')[0] && document.querySelectorAll('.pillar-panel')[0].classList.add('is-active');
    document.querySelectorAll('.pillar-item')[0] && document.querySelectorAll('.pillar-item')[0].classList.add('is-active');
  }

})();
