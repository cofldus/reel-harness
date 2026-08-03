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
    if (text) text.focus();
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-sample-story]");
    if (!button) return;
    event.preventDefault();
    fill(SAMPLES[Math.floor(Math.random() * SAMPLES.length)]);
  });
})();
