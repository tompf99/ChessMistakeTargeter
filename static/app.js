const form = document.querySelector("#form");
const message = document.querySelector("#message");
const results = document.querySelector("#results");
const targets = document.querySelector("#targets");
let line = [];

const pieces = {
  K: "♔", Q: "♕", R: "♖", B: "♗", N: "♘", P: "♙",
  k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟"
};

function drawBoard(fen) {
  const board = document.querySelector("#board");
  board.replaceChildren();
  fen.split(" ")[0].split("/").forEach((rank, row) => {
    let col = 0;
    for (const char of rank) {
      const count = /\d/.test(char) ? Number(char) : 1;
      for (let i = 0; i < count; i++, col++) {
        const square = document.createElement("span");
        square.className = `square ${(row + col) % 2 ? "dark" : "light"}`;
        square.textContent = pieces[char] || "";
        board.append(square);
      }
    }
  });
}

function isTarget(node) {
  return node.opponent_mistake && !node.our_mistake;
}

function hasTarget(node) {
  return isTarget(node) || node.children.some(hasTarget);
}

function displayScore(value) {
  return value == null ? "—" : `${value > 0 ? "+" : ""}${(value / 100).toFixed(2)}`;
}

function render() {
  const node = line[line.length - 1];
  drawBoard(node.fen);
  const path = document.querySelector("#path");
  path.replaceChildren();
  line.forEach((step, index) => {
    const button = document.createElement("button");
    button.textContent = index ? `${Math.ceil(index / 2)}${index % 2 ? "." : "..."} ${step.move}` : "Start";
    button.addEventListener("click", () => { line = line.slice(0, index + 1); render(); });
    path.append(button);
  });
  const body = document.querySelector("#moves");
  body.replaceChildren();
  const children = node.children.filter(child => !targets.checked || hasTarget(child));
  document.querySelector("#empty").hidden = children.length > 0;
  for (const child of children) {
    const row = document.createElement("tr");
    const moveCell = row.insertCell();
    const button = document.createElement("button");
    button.textContent = child.move;
    button.addEventListener("click", () => { line.push(child); render(); });
    moveCell.append(button);
    const status = child.our_mistake ? "Avoid" : isTarget(child) ? "Target" : "No mistake yet";
    const date = child.last_played ? new Date(child.last_played * 1000).toISOString().slice(0, 10) : "—";
    for (const value of [
      child.rank, child.engine_choice ? (child.games ? "Engine + played" : "Engine") : "Played",
      child.games, date, displayScore(child.evaluation),
      (child.loss / 100).toFixed(2), displayScore(child.swing), status
    ]) {
      row.insertCell().textContent = value;
    }
    row.lastElementChild.className = child.our_mistake ? "avoid" : isTarget(child) ? "target" : "";
    body.append(row);
  }
}

targets.addEventListener("change", render);
form.addEventListener("submit", async event => {
  event.preventDefault();
  const values = new FormData(form);
  const payload = {
    username: values.get("username"), color: values.get("color"),
    max_moves: Number(values.get("max_moves")),
    max_games: values.get("max_games") === "" ? null : Number(values.get("max_games")),
    depth: Number(values.get("depth")), threshold: Number(values.get("threshold"))
  };
  const submit = form.querySelector("button");
  submit.disabled = true;
  results.hidden = true;
  message.textContent = "Fetching games and analyzing positions. This can take several minutes.";
  try {
    const response = await fetch("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Analysis failed.");
    line = [data.tree];
    targets.checked = false;
    document.querySelector("#summary").textContent =
      `${data.username} as ${data.color}: ${data.tree.games} usable games from ${data.selected_games} selected; ${data.skipped_games} skipped. Depth ${data.depth}, player mistake threshold ${data.threshold} cp, up to ${data.max_moves} full moves.`;
    results.hidden = false;
    message.textContent = "";
    render();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
