const form = document.querySelector("#form");
const message = document.querySelector("#message");
const results = document.querySelector("#results");
const targets = document.querySelector("#targets");
let line = [];
let cursor = 0;

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
  return node.player_move && ["Mistake", "Blunder"].includes(node.status);
}

function hasTarget(node) {
  return isTarget(node) || node.children.some(hasTarget);
}

function displayScore(value) {
  return value == null ? "—" : `${value > 0 ? "+" : ""}${(value / 100).toFixed(2)}`;
}

function findRoute(root, moves) {
  const route = [root];
  let node = root;
  for (const move of moves) {
    node = node.children.find(child => child.move === move);
    if (!node) return null;
    route.push(node);
  }
  return route;
}

function render() {
  const node = line[cursor];
  drawBoard(node.fen);
  const score = node.evaluation || 0;
  const advantage = Math.min(Math.abs(score) / 600, 1) * 50;
  const fill = document.querySelector("#eval-fill");
  fill.style.bottom = score >= 0 ? "50%" : "";
  fill.style.top = score < 0 ? "50%" : "";
  fill.style.height = `${advantage}%`;
  fill.classList.toggle("negative", score < 0);
  document.querySelector("#eval-score").textContent = displayScore(node.evaluation);
  document.querySelector("#eval-bar").setAttribute(
    "aria-label", `${displayScore(node.evaluation)} from the selected player's perspective`
  );
  const opening = document.querySelector("#opening-name");
  opening.textContent = node.opening_name ? `Opening: ${node.opening_name}` : "";
  opening.hidden = !node.opening_name;
  const transpositions = document.querySelector("#transpositions");
  transpositions.replaceChildren();
  transpositions.hidden = node.transpositions.length === 0;
  if (node.transpositions.length) {
    const heading = document.createElement("strong");
    heading.textContent = `Same position reached by ${node.position_games} games via:`;
    const list = document.createElement("ul");
    for (const route of node.transpositions) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.textContent = `${route.line} (${route.games} ${route.games === 1 ? "game" : "games"})`;
      button.addEventListener("click", () => {
        const selectedRoute = findRoute(line[0], route.moves);
        if (selectedRoute) {
          line = selectedRoute;
          cursor = line.length - 1;
          render();
        }
      });
      item.append(button);
      list.append(item);
    }
    transpositions.append(heading, list);
  }
  const path = document.querySelector("#path");
  path.replaceChildren();
  line.forEach((step, index) => {
    const button = document.createElement("button");
    button.textContent = index ? `${Math.ceil(index / 2)}${index % 2 ? "." : "..."} ${step.move}` : "Start";
    button.setAttribute("aria-current", index === cursor ? "step" : "false");
    button.addEventListener("click", () => { cursor = index; render(); });
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
    button.addEventListener("click", () => {
      line = line.slice(0, cursor + 1);
      line.push(child);
      cursor++;
      render();
    });
    moveCell.append(button);
    const date = child.last_played ? new Date(child.last_played * 1000).toISOString().slice(0, 10) : "—";
    for (const value of [
      child.rank,
      child.engine_choice ? (child.games ? "Engine + played" : "Engine") : "Played",
      child.games, child.position_games, date, displayScore(child.evaluation),
      (child.loss / 100).toFixed(2), child.status
    ]) {
      row.insertCell().textContent = value;
    }
    row.lastElementChild.className = child.status.toLowerCase();
    body.append(row);
  }
}

targets.addEventListener("change", render);
document.addEventListener("keydown", event => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.target.closest("input, select, textarea, [contenteditable=true]")) return;
  if (event.key === "ArrowLeft" && cursor > 0) {
    event.preventDefault();
    cursor--;
    render();
  } else if (event.key === "ArrowRight" && cursor < line.length - 1) {
    event.preventDefault();
    cursor++;
    render();
  }
});
form.addEventListener("submit", async event => {
  event.preventDefault();
  const values = new FormData(form);
  const payload = {
    username: values.get("username"), color: values.get("color"),
    max_moves: Number(values.get("max_moves")),
    max_games: values.get("max_games") === "" ? null : Number(values.get("max_games")),
    depth: Number(values.get("depth"))
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
    cursor = 0;
    targets.checked = false;
    document.querySelector("#summary").textContent =
      `${data.username} as ${data.color}: ${data.tree.position_games} usable games from ${data.selected_games} selected; ${data.skipped_games} skipped. Depth ${data.depth}, up to ${data.max_moves} full moves.`;
    results.hidden = false;
    message.textContent = "";
    render();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
