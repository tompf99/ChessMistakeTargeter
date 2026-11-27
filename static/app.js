const form = document.querySelector("#form");
const message = document.querySelector("#message");
const results = document.querySelector("#results");
let line = [];
let cursor = 0;
let settings = null;
let selectedSquare = null;
let moving = false;

const pieces = {
  K: "♔", Q: "♕", R: "♖", B: "♗", N: "♘", P: "♙",
  k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟"
};

function drawBoard(fen) {
  const board = document.querySelector("#board");
  board.replaceChildren();
  const turn = fen.split(" ")[1];
  fen.split(" ")[0].split("/").forEach((rank, row) => {
    let col = 0;
    for (const char of rank) {
      const count = /\d/.test(char) ? Number(char) : 1;
      for (let i = 0; i < count; i++, col++) {
        const square = document.createElement("button");
        square.type = "button";
        square.className = `square ${(row + col) % 2 ? "dark" : "light"}`;
        square.dataset.square = `${"abcdefgh"[col]}${8 - row}`;
        square.dataset.piece = pieces[char] ? char : "";
        square.dataset.movable = pieces[char] && (char === char.toUpperCase()) === (turn === "w") ? "true" : "false";
        square.setAttribute("aria-label", `${square.dataset.square}${pieces[char] ? ` ${char}` : ""}`);
        square.classList.toggle("selected", selectedSquare === square.dataset.square);
        square.textContent = pieces[char] || "";
        if (col === 0 || row === 7) {
          const label = document.createElement("span");
          label.className = `coordinate ${col === 0 ? "rank-label" : "file-label"}`;
          label.setAttribute("aria-hidden", "true");
          label.textContent = col === 0 ? String(8 - row) : "abcdefgh"[col];
          square.append(label);
          if (col === 0 && row === 7) {
            const file = document.createElement("span");
            file.className = "coordinate file-label";
            file.setAttribute("aria-hidden", "true");
            file.textContent = "a";
            square.append(file);
          }
        }
        board.append(square);
      }
    }
  });
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 8 8");
  svg.classList.add("arrows");
  svg.setAttribute("aria-hidden", "true");
  board.append(svg);
}

function showArrow(uci) {
  const svg = document.querySelector("#board .arrows");
  if (!svg) return;
  svg.replaceChildren();
  if (!uci || uci.length < 4) return;
  const coordinates = square => ({
    x: "abcdefgh".indexOf(square[0]) + 0.5,
    y: 8 - Number(square[1]) + 0.5,
  });
  const from = coordinates(uci.slice(0, 2));
  const to = coordinates(uci.slice(2, 4));
  const dx = to.x - from.x, dy = to.y - from.y;
  const length = Math.hypot(dx, dy);
  if (!length) return;
  const ux = dx / length, uy = dy / length;
  const shaft = document.createElementNS(svg.namespaceURI, "line");
  shaft.setAttribute("x1", from.x);
  shaft.setAttribute("y1", from.y);
  shaft.setAttribute("x2", to.x - ux * 0.25);
  shaft.setAttribute("y2", to.y - uy * 0.25);
  const head = document.createElementNS(svg.namespaceURI, "polygon");
  head.setAttribute("points", `${to.x},${to.y} ${to.x - ux * 0.7 - uy * 0.3},${to.y - uy * 0.7 + ux * 0.3} ${to.x - ux * 0.7 + uy * 0.3},${to.y - uy * 0.7 - ux * 0.3}`);
  svg.append(shaft, head);
}

function selectMove(child) {
  line = line.slice(0, cursor + 1);
  line.push(child);
  cursor++;
  selectedSquare = null;
  document.querySelector("#board-message").textContent = "";
  render();
}

async function movePiece(from, to) {
  if (moving || from === to) return;
  const node = line[cursor];
  const source = document.querySelector(`[data-square="${from}"]`);
  const piece = source?.dataset.piece;
  if (!piece || source.dataset.movable !== "true") return;
  selectedSquare = null;
  drawBoard(node.fen);
  let uci = from + to;
  if ((piece === "P" && to[1] === "8") || (piece === "p" && to[1] === "1")) {
    const dialog = document.querySelector("#promotion");
    dialog.returnValue = "";
    dialog.showModal();
    const choice = await new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true }));
    if (!choice || node !== line[cursor]) return;
    uci += choice;
  }
  const known = node.children.find(child => child.uci === uci);
  if (known) {
    selectMove(known);
    return;
  }
  moving = true;
  const boardMessage = document.querySelector("#board-message");
  boardMessage.textContent = "Analyzing move…";
  try {
    const response = await fetch("/api/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen: node.fen, uci, color: settings.color, depth: settings.depth, max_moves: settings.max_moves })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not analyze the move.");
    if (line[cursor] === node) {
      node.children.push(data);
      node.children.sort((a, b) => a.loss - b.loss || a.uci.localeCompare(b.uci));
      node.children.forEach((child, index) => { child.rank = index + 1; });
      selectMove(data);
    }
  } catch (error) {
    if (line[cursor] === node) boardMessage.textContent = error.message;
  } finally {
    moving = false;
  }
}

function displayScore(value) {
  return value == null ? "—" : `${value > 0 ? "+" : ""}${(value / 100).toFixed(2)}`;
}

function whiteScore(value) {
  return value == null ? null : settings.color === "white" ? value : -value;
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

function findUciRoute(root, moves) {
  const route = [root];
  let node = root;
  for (const move of moves) {
    node = node.children.find(child => child.uci === move);
    if (!node) return null;
    route.push(node);
  }
  return route;
}

function showTab(name) {
  for (const tab of ["explorer", "interest"]) {
    const selected = tab === name;
    document.querySelector(`#${tab}`).hidden = !selected;
    const button = document.querySelector(`#${tab}-tab`);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
}

function renderInterest(items) {
  const body = document.querySelector("#interest-lines");
  body.replaceChildren();
  document.querySelector("#interest-empty").hidden = items.length > 0;
  for (const item of items) {
    const row = body.insertRow();
    const button = document.createElement("button");
    button.textContent = item.line;
    button.addEventListener("click", () => {
      const selectedRoute = findUciRoute(line[0], item.moves);
      if (!selectedRoute) {
        message.textContent = "The recorded line is no longer available.";
        return;
      }
      line = selectedRoute;
      cursor = line.length - 1;
      showTab("explorer");
      render();
    });
    row.insertCell().append(button);
    const date = item.last_played ? new Date(item.last_played * 1000).toISOString().slice(0, 10) : "—";
    for (const value of [item.games, date, displayScore(whiteScore(item.evaluation))]) {
      row.insertCell().textContent = value;
    }
  }
}

function render() {
  const node = line[cursor];
  selectedSquare = null;
  document.querySelector("#board-message").textContent = "";
  drawBoard(node.fen);
  const score = whiteScore(node.evaluation);
  const whiteShare = 50 + 50 * Math.tanh(score / 400);
  const fill = document.querySelector("#eval-fill");
  fill.style.height = `${whiteShare}%`;
  const evalScore = document.querySelector("#eval-score");
  evalScore.textContent = Math.abs(score) >= 99000 ? (score > 0 ? "+M" : "−M") : displayScore(score);
  evalScore.classList.toggle("black-advantage", score < 0);
  document.querySelector("#eval-bar").setAttribute(
    "aria-label", `${evalScore.textContent}, positive favors White`
  );
  const opening = document.querySelector("#opening-name");
  const openingName = line.slice(0, cursor + 1).reverse().find(step => step.opening_name)?.opening_name;
  opening.textContent = openingName ? `Opening: ${openingName}` : "";
  opening.hidden = !openingName;
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
  const children = node.children;
  document.querySelector("#empty").hidden = children.length > 0;
  for (const child of children) {
    const row = document.createElement("tr");
    const moveCell = row.insertCell();
    const button = document.createElement("button");
    button.textContent = child.move;
    button.addEventListener("click", () => selectMove(child));
    row.addEventListener("mouseenter", () => showArrow(child.uci));
    row.addEventListener("mouseleave", () => showArrow(null));
    button.addEventListener("focus", () => showArrow(child.uci));
    button.addEventListener("blur", () => showArrow(null));
    moveCell.append(button);
    const date = child.last_played ? new Date(child.last_played * 1000).toISOString().slice(0, 10) : "—";
    for (const value of [
      child.rank,
      child.engine_choice ? (child.games ? "Engine + played" : "Engine") : "Played",
      child.games, child.position_games, date, displayScore(whiteScore(child.evaluation)),
      (child.loss / 100).toFixed(2), child.status
    ]) {
      row.insertCell().textContent = value;
    }
    row.lastElementChild.className = child.status.toLowerCase();
    body.append(row);
  }
}

for (const tab of ["explorer", "interest"]) {
  document.querySelector(`#${tab}-tab`).addEventListener("click", () => showTab(tab));
}
document.querySelector(".tabs").addEventListener("keydown", event => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const next = event.target.id === "explorer-tab" ? "interest" : "explorer";
  if (!event.target.matches('[role="tab"]')) return;
  event.preventDefault();
  event.stopPropagation();
  showTab(next);
  document.querySelector(`#${next}-tab`).focus();
});
const board = document.querySelector("#board");
let pointerStart = null;
board.addEventListener("pointerdown", event => {
  const square = event.target.closest(".square");
  if (!square || event.button !== 0 || moving) return;
  pointerStart = { square: square.dataset.square, x: event.clientX, y: event.clientY, piece: square.dataset.movable === "true" ? pieces[square.dataset.piece] : "", ghost: null };
  square.setPointerCapture(event.pointerId);
  event.preventDefault();
});
board.addEventListener("pointermove", event => {
  if (!pointerStart || !pointerStart.piece || Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) <= 5) return;
  if (!pointerStart.ghost) {
    pointerStart.ghost = document.createElement("span");
    pointerStart.ghost.className = "drag-piece";
    pointerStart.ghost.textContent = pointerStart.piece;
    document.body.append(pointerStart.ghost);
  }
  pointerStart.ghost.style.left = `${event.clientX}px`;
  pointerStart.ghost.style.top = `${event.clientY}px`;
});
board.addEventListener("pointerup", event => {
  if (!pointerStart) return;
  const target = document.elementFromPoint(event.clientX, event.clientY)?.closest(".square");
  const from = pointerStart.square;
  const isDrag = Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 5;
  pointerStart.ghost?.remove();
  pointerStart = null;
  if (!target) return;
  const to = target.dataset.square;
  if (isDrag) {
    movePiece(from, to);
  } else if (selectedSquare && selectedSquare !== to) {
    const source = selectedSquare;
    selectedSquare = null;
    movePiece(source, to);
  } else {
    selectedSquare = target.dataset.movable === "true" && selectedSquare !== to ? to : null;
    drawBoard(line[cursor].fen);
  }
});
board.addEventListener("pointercancel", () => { pointerStart?.ghost?.remove(); pointerStart = null; });
board.addEventListener("click", event => {
  if (event.detail !== 0) return;
  const target = event.target.closest(".square");
  if (!target) return;
  if (selectedSquare && selectedSquare !== target.dataset.square) {
    const from = selectedSquare;
    selectedSquare = null;
    movePiece(from, target.dataset.square);
  } else {
    selectedSquare = target.dataset.movable === "true" ? target.dataset.square : null;
    drawBoard(line[cursor].fen);
  }
});
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

function updateProgress(event) {
  const fetchBar = document.querySelector("#fetch-progress");
  const analysisBar = document.querySelector("#analysis-progress");
  if (event.phase === "fetch") {
    const fetchLabel = document.querySelector("#fetch-label");
    if (event.total == null) {
      fetchBar.removeAttribute("value");
      fetchLabel.textContent = "Fetching archive list…";
    } else {
      fetchBar.max = Math.max(event.total, 1);
      fetchBar.value = event.total ? event.completed : 1;
      fetchLabel.textContent = `Fetching archives: ${event.completed} of ${event.total}`;
    }
  } else if (event.phase === "prepare") {
    document.querySelector("#analysis-label").textContent = "Preparing games…";
  } else if (event.phase === "analysis") {
    analysisBar.max = Math.max(event.total, 1);
    analysisBar.value = event.completed;
    document.querySelector("#analysis-label").textContent = event.completed === event.total
      ? `Analyzed ${event.completed} of ${event.total} games`
      : `Analyzing game ${event.completed + 1} of ${event.total} (${event.completed} complete)`;
  }
}

async function readAnalysis(response) {
  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.error || "Analysis failed.");
  }
  if (!response.body) throw new Error("Streaming analysis is unavailable in this browser.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  while (true) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value, {stream: !done});
    const rows = buffer.split("\n");
    buffer = rows.pop();
    for (const row of rows) {
      if (!row) continue;
      const event = JSON.parse(row);
      if (event.type === "progress") updateProgress(event);
      if (event.type === "error") throw new Error(event.error);
      if (event.type === "result") result = event.data;
    }
    if (done) break;
  }
  if (buffer.trim() || !result) throw new Error("Analysis ended before results were received.");
  return result;
}

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
  message.textContent = "";
  const progress = document.querySelector("#progress");
  progress.hidden = false;
  document.querySelector("#fetch-progress").removeAttribute("value");
  document.querySelector("#analysis-progress").removeAttribute("value");
  document.querySelector("#fetch-label").textContent = "Fetching archive list…";
  document.querySelector("#analysis-label").textContent = "Waiting to analyze games";
  try {
    const response = await fetch("/api/analyze/stream", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    });
    const data = await readAnalysis(response);
    line = [data.tree];
    cursor = 0;
    settings = payload;
    showTab("explorer");
    renderInterest(data.lines_of_interest);
    document.querySelector("#summary").textContent =
      `${data.username} as ${data.color}: ${data.tree.position_games} usable games from ${data.selected_games} selected; ${data.skipped_games} skipped. Depth ${data.depth}, up to ${data.max_moves} full moves.`;
    results.hidden = false;
    message.textContent = "";
    render();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    submit.disabled = false;
    progress.hidden = true;
  }
});
