// Disables a form's submit button on submit -- prevents a double-click from
// firing two requests. The server-side idempotency_key (see job_new.html)
// is the real dedup guarantee; this is just UX polish so a slow request
// doesn't invite a second click.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!form.hasAttribute("data-disable-on-submit")) return;
  var button = form.querySelector('button[type="submit"]');
  if (button) {
    button.disabled = true;
    button.dataset.originalText = button.textContent;
    button.textContent = "처리 중...";
  }
});

// "예시 이야기" -- fills the new-project form with a sample so the first
// run is one click rather than a blank page and a blinking cursor. The
// samples live here rather than on the server because they are pure UI
// affordance: nothing is created, nothing is charged, and the user is
// expected to edit or replace whatever lands in the box.
(function () {
  var SAMPLES = [
    {
      title: "비 오는 밤",
      genre: "drama",
      tone: "quiet tension",
      text: "그날 밤, 지우는 호텔 창밖의 비를 오래 바라보았다. 전화벨이 울렸지만 받지 않았다. " +
            "마침내 그녀는 천천히 문 쪽으로 돌아섰다.",
    },
    {
      title: "마지막 승객",
      genre: "mystery",
      tone: "bleak",
      text: "막차의 승객은 그 사람 하나였다. 기사는 백미러로 몇 번이나 뒷좌석을 확인했다. " +
            "종점에 도착했을 때, 좌석은 비어 있었고 창문에는 손자국만 남아 있었다.",
    },
    {
      title: "아침의 편지",
      genre: "romance",
      tone: "warm",
      text: "이사한 집의 우편함에는 이전 주인 앞으로 온 편지가 쌓여 있었다. " +
            "그는 한 통을 열어보았고, 그날부터 매주 답장을 썼다. 보낼 곳도 모른 채.",
    },
    {
      title: "정전",
      genre: "thriller",
      tone: "tense",
      text: "복도의 불이 한 층씩 꺼지고 있었다. 그는 계단을 올랐다. " +
            "위층 문 앞에 도착했을 때, 안에서 누군가 잠금장치를 여는 소리가 났다.",
    },
  ];

  function fill(sample) {
    var title = document.getElementById("title");
    var text = document.getElementById("source_text");
    if (title) title.value = sample.title;
    if (text) text.value = sample.text;
    ["genre", "tone"].forEach(function (name) {
      var select = document.getElementById(name);
      if (select && sample[name]) select.value = sample[name];
    });
    // Dispatched so the writing checklist and the length note react to a
    // programmatic fill exactly as they do to typing.
    if (text) {
      text.dispatchEvent(new Event("input", { bubbles: true }));
      text.focus();
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-sample-story]");
    if (!button) return;
    event.preventDefault();
    fill(SAMPLES[Math.floor(Math.random() * SAMPLES.length)]);
  });
})();

// Live writing checklist on the new-project form.
//
// The single biggest lever on output quality is the source text, and the
// things that matter are not guessable: a character's clothes are what
// the reference image gets built from, summarised speech produces no
// dialogue line, an inner state no camera can see produces no shot. A
// static help panel gets skimmed once and forgotten; a checklist that
// responds while you type surfaces each point at the moment it is
// actionable.
//
// These are keyword heuristics and nothing more. They are advisory, they
// never gate submission, and the UI says so -- a false negative here must
// read as "maybe consider this", never as "your story is wrong". Erring
// toward under-detecting is deliberate: a box that ticks too easily
// teaches nothing.
(function () {
  var TESTS = {
    // Quotation marks of any of the forms Korean prose actually uses.
    dialogue: function (t) { return /[“”"'‘’「」『』]/.test(t) && /[“"'‘「『][^”"'’」』]{2,}/.test(t); },
    // An age, or something worn//visible on a body.
    character: function (t) {
      return /(\d+\s*(살|세|대)|스무|서른|마흔|쉰|소년|소녀|청년|노인|중년)/.test(t) ||
             /(머리|눈빛|눈동자|코트|재킷|셔츠|치마|바지|신발|안경|모자|장갑|목도리|가방|옷차림|맨발|수염|흉터)/.test(t);
    },
    // A place, a time of day, or a light source.
    place: function (t) {
      return /(새벽|아침|낮|저녁|밤|자정|정오|해질|노을|어둠|불빛|가로등|네온|형광등|촛불|햇빛|달빛|그림자)/.test(t) ||
             /(방|거리|골목|계단|복도|창가|창밖|부엌|옥상|정류장|기차|버스|카페|호텔|공원|해변|숲|병원|교실)/.test(t);
    },
    // A physical verb -- something a lens could record.
    action: function (t) {
      return /(집어|내려놓|일어|앉|걸어|달려|뛰|돌아서|멈춰|열|닫|꺼내|넣|접|찢|던지|밀|당기|쥐|놓|건네|올려다|내려다|고개를|손을|발을)/.test(t);
    },
    // A pivot word, or an explicit passage of time.
    turn: function (t) {
      return /(그러나|하지만|그런데|마침내|결국|그때|갑자기|한참|이윽고|비로소|그제야|처음으로|더는|다시는)/.test(t);
    },
  };

  function update() {
    var box = document.querySelector("[data-story]");
    var list = document.querySelector("[data-checklist]");
    if (!box) return;

    var text = box.value;
    if (list) {
      list.querySelectorAll(".check").forEach(function (item) {
        var test = TESTS[item.dataset.check];
        item.classList.toggle("check-on", Boolean(test && test(text)));
      });
    }

    var count = document.querySelector("[data-char-count]");
    if (count) count.textContent = text.length;

    // Length guidance, because both ends genuinely fail: too little and
    // the adaptation invents what is not there, too much and the useful
    // detail is diluted across more shots than the target length allows.
    var note = document.querySelector("[data-length-note]");
    if (note) {
      if (!text.length) note.textContent = "";
      else if (text.length < 150) note.textContent = "· 조금 짧습니다. 200자쯤이면 장면이 또렷해집니다.";
      else if (text.length <= 1200) note.textContent = "· 적당합니다.";
      else note.textContent = "· 깁니다. 핵심 장면만 남기면 각 샷이 선명해집니다.";
    }
  }

  document.addEventListener("input", function (event) {
    if (event.target.closest("[data-story]")) update();
  });
  // Each guide item expands to its own why-and-example pair on demand:
  // five explanations open at once is a wall of text nobody reads.
  document.addEventListener("click", function (event) {
    var label = event.target.closest(".check-label");
    if (!label) return;
    var detail = label.parentElement.querySelector(".check-detail");
    if (!detail) return;
    var open = detail.hidden;
    detail.hidden = !open;
    label.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("DOMContentLoaded", update);
  if (document.readyState !== "loading") update();
})();

// Shot-count estimate on the new-project form. "목표 길이" is an
// abstraction; shots are what actually get generated and billed, and the
// relationship (8s per reference-driven shot, one paid generation per
// candidate) is not something a first-time user can be expected to derive
// from a dropdown. Advisory only -- the server recomputes the real plan
// during adaptation, so this never becomes a promise.
(function () {
  var SHOT_SECONDS = 8;

  function update() {
    var out = document.querySelector("[data-estimate]");
    var duration = document.querySelector("[data-duration]");
    if (!out || !duration) return;

    var seconds = parseInt(duration.value, 10);
    if (!seconds) { out.textContent = ""; return; }

    var takesField = document.querySelector("[data-takes]");
    var takes = takesField ? parseInt(takesField.value, 10) : 0;
    var shots = Math.round(seconds / SHOT_SECONDS);
    var text = "이 설정이면 약 " + shots + "개 샷이 생성됩니다 (샷당 " + SHOT_SECONDS + "초).";
    if (takes > 0) {
      text += " 후보를 " + takes + "개씩 만들면 유료 생성은 " + shots * takes + "번입니다.";
    }
    out.textContent = text;
  }

  document.addEventListener("change", function (event) {
    if (event.target.closest("[data-duration], [data-takes]")) update();
  });
  document.addEventListener("DOMContentLoaded", update);
  if (document.readyState !== "loading") update();
})();

// Project-list filtering. Client-side on purpose: this is a single-user
// local tool whose list is tens of rows, not thousands, so a round trip
// per keystroke would be slower without being more correct.
(function () {
  function apply() {
    var toolbar = document.querySelector("[data-project-filter]");
    var list = document.querySelector("[data-project-list]");
    if (!toolbar || !list) return;

    var active = toolbar.querySelector('.segment[aria-pressed="true"]');
    var segment = active ? active.dataset.segment : "all";
    var search = toolbar.querySelector("[data-project-search]");
    var query = search ? search.value.trim().toLowerCase() : "";

    var shown = 0;
    list.querySelectorAll(".project-card").forEach(function (card) {
      var matchesGroup = segment === "all" || card.dataset.group === segment;
      var matchesQuery = !query || (card.dataset.title || "").indexOf(query) !== -1;
      var visible = matchesGroup && matchesQuery;
      card.hidden = !visible;
      if (visible) shown++;
    });

    var empty = document.querySelector("[data-empty-filter]");
    if (empty) empty.hidden = shown !== 0;
  }

  document.addEventListener("click", function (event) {
    var segment = event.target.closest(".segment");
    if (!segment) return;
    segment.parentElement.querySelectorAll(".segment").forEach(function (other) {
      other.setAttribute("aria-pressed", String(other === segment));
    });
    apply();
  });
  document.addEventListener("input", function (event) {
    if (event.target.closest("[data-project-search]")) apply();
  });
})();

// The detail page polls a small status fragment, but the rest of the page
// -- storyboard, casting, the now panel -- was rendered against the old
// status and goes stale the moment the project advances. When the poll
// reports a status different from the one the page was built for, reload
// so what you see matches what the project actually is.
(function () {
  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (!event.target || event.target.id !== "fable-status") return;
    var page = document.querySelector("[data-project-status]");
    if (!page) return;
    if (event.target.dataset.status !== page.dataset.projectStatus) window.location.reload();
  });
})();

/* Click a take to see it big.
 *
 * The grid crops thumbnails to fill their tile, which makes a contact
 * sheet readable and makes judging a shot impossible -- you cannot decide
 * between four takes from four cropped postage stamps. Clicking opens the
 * whole 9:16 frame, uncropped.
 *
 * Uses <dialog>, so Escape and focus handling come from the platform
 * rather than from hand-rolled key listeners. */
(function () {
  var dialog = document.getElementById("take-lightbox");
  if (!dialog) return;
  var player = dialog.querySelector("video");
  var caption = dialog.querySelector("[data-lightbox-caption]");

  document.addEventListener("click", function (event) {
    var source = event.target.closest("video[data-enlarge]");
    if (!source) return;
    event.preventDefault();
    player.src = source.getAttribute("src");
    if (caption) caption.textContent = source.getAttribute("data-caption") || "";
    dialog.showModal();
    player.play().catch(function () {
      /* Autoplay may be refused; the controls still work. */
    });
  });

  function close() {
    player.pause();
    // Releasing the source stops the browser buffering a clip nobody is
    // watching once the dialog is shut.
    player.removeAttribute("src");
    player.load();
  }
  dialog.addEventListener("close", close);
  dialog.addEventListener("click", function (event) {
    // Clicking the backdrop (the dialog element itself, not its contents)
    // closes -- the gesture everyone expects from a lightbox.
    if (event.target === dialog) dialog.close();
  });
})();
