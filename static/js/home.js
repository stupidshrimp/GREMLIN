(function () {
  "use strict";

  // A card a viewer cannot open is still on the page, and clicking it has to
  // say why rather than do nothing. The wording is rendered into the card by
  // the template, because what to do next depends on whether anybody is signed
  // in: a guest needs the login, a viewer needs an administrator.
  //
  // More than one card can be locked at once -- signed out, Perform an Analysis
  // is locked as well as Disposition -- so every one of them is wired, not just
  // the first.
  var cards = document.querySelectorAll(".life-data-card-locked");

  Array.prototype.forEach.call(cards, function (card) {
    card.addEventListener("click", function () {
      var message = card.getAttribute("data-locked-message");
      if (message && window.gremlinToast) {
        window.gremlinToast(message);
      }
    });
  });
})();

(function () {
  "use strict";

  // Each workflow card has an animation that runs only while it is being
  // pointed at. Assigning `src` is what starts it and removing `src` is what
  // stops it, because for a GIF there is nothing else: it has no playback API,
  // and hiding it is not enough. Chromium keeps a `visibility: hidden` GIF
  // advancing -- hide one of these for 2400ms and it comes back 21 frames on,
  // not where it left -- so an image parked at zero opacity would go on
  // decoding 20 frames a second for the life of the page, three of them at
  // once, for nobody. The source is therefore held only for as long as the card
  // is.
  //
  // Two things fall out of that, both wanted. The first hover is also the first
  // download, so the home page itself fetches none of the three -- they run
  // from 650 KB to 2 MB -- and a visitor who never reaches for a card never
  // pays for one. And every hover after that starts the animation from its
  // first frame rather than resuming mid-stride, off the HTTP cache, so there
  // is no second download and no wait.
  //
  // `data-hover-src` is the lasting record of which file belongs to which card;
  // `src` is only ever the live one.
  var HOLD_MS = 260;

  // The stylesheet refuses to reveal the animation to anyone who has asked for
  // reduced motion, and there is no reason to spend their bandwidth on a file
  // they will not be shown. Read at hover rather than at load, so that turning
  // the preference off part way through a session is enough to get the
  // animations without a reload.
  var stillness = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;

  var media = document.querySelectorAll(".card-hover-media[data-hover-src]");
  var resyncs = [];

  Array.prototype.forEach.call(media, function (image) {
    var card = image.closest(".life-data-card");
    var source = image.getAttribute("data-hover-src");
    if (!card || !source) {
      return;
    }

    var release = null;

    function play() {
      window.clearTimeout(release);
      if (stillness && stillness.matches) {
        return;
      }
      // Pointer and focus can both be on the same card; whichever arrives
      // second must not restart what the first one began.
      if (!image.getAttribute("src")) {
        image.src = source;
      }
    }

    function stop() {
      window.clearTimeout(release);
      // Waits out the fade the stylesheet is running. Dropping the source at
      // the moment the pointer leaves would blank the artwork instantly and
      // there would be nothing left to fade.
      release = window.setTimeout(function () {
        // The pointer leaving is not the same as the card being finished with.
        // A card tabbed to and then brushed past with the mouse gets a
        // `pointerleave` while it still holds focus -- and the stylesheet is
        // still showing the animation, so releasing here would empty the
        // card in front of someone looking at it. This is the same pair of
        // states the reveal is written against, asked the other way round.
        if (card.matches(":hover, :focus-visible")) {
          return;
        }
        image.removeAttribute("src");
      }, HOLD_MS);
    }

    // The preference can change while a card is already under the pointer or
    // holding focus, and neither `play` nor `stop` will run again to notice:
    // hovering is what asks the question, and the hovering already happened. So
    // the answer is brought to the card instead. Turning stillness on takes the
    // source away mid-hover; turning it off gives it back, rather than leaving
    // the card revealed and empty until the pointer has left and returned.
    function resync() {
      if (stillness.matches) {
        window.clearTimeout(release);
        image.removeAttribute("src");
      } else if (card.matches(":hover, :focus-visible")) {
        play();
      }
    }

    resyncs.push(resync);

    // `pointerenter` rather than `mouseenter` so a pen or a finger landing on
    // the card counts the same as a cursor. Focus is the keyboard's equivalent:
    // the card reveals its extra copy on :focus-visible, and the animation is
    // written to come with it.
    card.addEventListener("pointerenter", play);
    card.addEventListener("focus", play);
    card.addEventListener("pointerleave", stop);
    card.addEventListener("blur", stop);
  });

  if (stillness && stillness.addEventListener) {
    stillness.addEventListener("change", function () {
      for (var i = 0; i < resyncs.length; i += 1) {
        resyncs[i]();
      }
    });
  }
})();

(function () {
  "use strict";

  // Home's "Show me around": where things are in GREMLIN as a whole, since this
  // is the page everyone lands on. The walk-through itself is page_tour.js; this
  // only says where it stops. It opens by itself on a first visit to Home on a
  // browser, and the button on the welcome card brings it back.
  //
  // A step whose target isn't drawn is left out when the tour starts: the
  // sidebar's fold button is hidden on narrow screens, where the sidebar sits
  // above the page instead of beside it.
  var tour = window.gremlinTour;
  var button = document.getElementById("home-tour-btn");
  if (!tour || !button) {
    return;
  }

  var SEEN_KEY = "gremlin.home.tour-seen";

  // The same test global_search.js makes for its shortcut badge.
  var isApple = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || "");

  // Which workflow cards there are depends on who is looking: an account kept
  // out of Life Data Analysis gets neither of its two, so the step only names
  // the cards on the page.
  function cardsBody() {
    var parts = [];
    if (document.getElementById("home-card-analysis")) {
      parts.push("Perform an Analysis fits failure data for one asset.");
    }
    if (document.getElementById("home-card-disposition")) {
      parts.push("Disposition sorts work orders and PM resets into what an analysis uses.");
    }
    if (document.getElementById("home-card-standards")) {
      parts.push("Standards and Documentation holds the references you work to.");
    }
    if (document.querySelector("#home-cards .life-data-card-locked")) {
      parts.push("A card you can't open yet says why when clicked.");
    }
    return parts.join(" ");
  }

  // The login form is only drawn for somebody who isn't signed in.
  function signedIn() {
    return !document.getElementById("loginForm");
  }

  var STEPS = [
    {
      target: ".home-hero-copy",
      title: "Welcome to GREMLIN",
      body:
        "GREMLIN turns the plant's CMMS work orders and PMs into reliability analysis, metrics " +
        "and a PM calendar. This tour points out where everything is. Skip or Esc ends it at any time.",
    },
    {
      target: ".sidebar-nav",
      title: "Every page, in one list",
      body:
        "The sidebar lists GREMLIN's pages, grouped by what they're for. A page that needs you to " +
        "log in is struck through until you do, and an hourglass marks one that is still being built.",
    },
    {
      target: "#sidebarToggle",
      title: "Make more room",
      body:
        "This folds the sidebar down to a strip of icons; point at an icon for its name. GREMLIN " +
        "remembers which way you left it.",
    },
    {
      target: "#globalSearch",
      title: "Search for anything",
      body: function () {
        return (
          "Type the name of any page, or of something on one such as an analysis, a chart or a " +
          "formula, and press Enter to go straight to it. " +
          (isApple ? "⌘K" : "Ctrl K") +
          " or / jumps here from any page."
        );
      },
    },
    {
      target: ".topbar-tools",
      title: "Links, light and dark",
      body:
        "The grid opens quick links to the other systems you use alongside GREMLIN, each in a new " +
        "tab. The sun and moon switch between the light and dark themes, and the bell is where " +
        "notifications will appear.",
    },
    {
      target: "#home-cards",
      title: "Start a piece of work",
      body: cardsBody,
    },
    {
      target: "#accountButton",
      title: "Your account",
      body: function () {
        return signedIn()
          ? "Who you're signed in as, and your role. Click it to see what your account covers or " +
              "to log out. The login lasts until the browser closes."
          : "Log in here to open the pages that need an account and to make changes. The login " +
              "lasts until the browser closes.";
      },
    },
    {
      target: ".site-footer",
      title: "Help, and news",
      body:
        "At the foot of every page: Patch Notes for what has changed, Report a Bug when something " +
        "isn't right, and Help for who to ask.",
    },
    {
      target: "#home-tour-btn",
      title: "Come back any time",
      body: "The tour only opens by itself once. Press Show me around to take it again.",
    },
  ];

  function start() {
    tour.start(STEPS, { seenKey: SEEN_KEY });
  }

  button.addEventListener("click", start);

  // First visit on this browser: once the page has drawn, so the spotlight
  // lands where the cards end up. Not over a dialog someone has already opened.
  if (!tour.seen(SEEN_KEY)) {
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        if (!tour.busy()) {
          start();
        }
      });
    });
  }
})();
