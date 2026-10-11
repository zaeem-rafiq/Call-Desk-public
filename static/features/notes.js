// notes feature module: NOTE cards (comments without a new forecast number) wait behind one "Show N notes" row
// under the main cards, so the cards that change a forecast stay in view. It works on the #feed DOM only:
// app.js keeps NOTE cards (class "compact") below the main cards and re-inserts a card when it changes group.

const feed = document.getElementById("feed");
let open = false;
let row = null;

function makeRow() {
  const li = document.createElement("li");
  li.className = "notes-toggle";
  const button = document.createElement("button");
  button.type = "button";
  button.title = "Comments from the call that do not give a new forecast number";
  button.addEventListener("click", () => {
    open = !open;
    update();
  });
  li.append(button);
  return li;
}

function update() {
  const cards = [...feed.children].filter((c) => c.classList.contains("flag"));
  const notes = cards.filter((c) => c.classList.contains("compact"));
  if (!cards.length) open = false;  // a new run starts with the notes folded
  // Until the first main card arrives the notes are the only news, so they stay visible (no empty pane at the start).
  const main = cards.length - notes.length;
  feed.classList.toggle("notes-hidden", !open && main > 0);
  if (!notes.length || !main) {
    row?.remove();
    return;
  }
  row ||= makeRow();
  if (row.nextElementSibling !== notes[0]) feed.insertBefore(row, notes[0]);
  const n = notes.length;
  const button = row.firstElementChild;
  button.textContent = open ? `Hide ${n === 1 ? "the note" : "notes"}` : `Show ${n} ${n === 1 ? "note" : "notes"}`;
  button.setAttribute("aria-expanded", String(open));
}

// Cards are added, moved and removed as direct children of #feed. The observer already batches a burst of changes
// into one call (not requestAnimationFrame, which a hidden tab pauses); moving the row itself settles on the next call.
if (feed) {
  new MutationObserver(update).observe(feed, { childList: true });
  update();
}
