/**
 * AI闂備浇妗ㄩ懗鑸垫櫠濡も偓閻ｅ灚绗熼埀顒€鐣峰璺哄耿婵☆垰鐨烽崑鎾寸鐎ｎ亞顦梺鍐叉惈閸婃悂路?- 闂備礁鎲￠幐鍝ョ矓閸洦鏁傛い鎺戝閸ゅ倸鈹戦悩鎻掆偓鑸电椤栫偞鈷戞い鎰剁稻椤绱?(v3)
 * 婵犳鍠楃换鎰不閹烘鐒垫い鎴ｆ硶椤︼妇绱掑璇蹭壕闂傚鍋勫ù鍌炲磻閸℃稒鍋ら柣鎴炵磾P
 */

// ============================================================
// SessionManager
// ============================================================
const SessionManager = {
    STORAGE_KEY: "skadi_sessions",
    getAll: function() {
        try { return JSON.parse(localStorage.getItem(this.STORAGE_KEY) || "[]"); }
        catch(e) { return []; }
    },
    _save: function(sessions) { localStorage.setItem(this.STORAGE_KEY, JSON.stringify(sessions)); },
    getCurrentId: function() {
        var id = localStorage.getItem("skadi_current_session");
        return id || this.create();
    },
    setCurrentId: function(id) { localStorage.setItem("skadi_current_session", id); },
    create: function() {
        var id = "session-" + Date.now();
        var sessions = this.getAll();
        sessions.unshift({ id: id, title: "\u65b0\u4f1a\u8bdd", created: new Date().toISOString(), messages: [], messageCount: 0 });
        this._save(sessions);
        this.setCurrentId(id);
        return id;
    },
    delete: function(id) {
        var sessions = this.getAll().filter(function(s) { return s.id !== id; });
        this._save(sessions);
        if (this.getCurrentId() === id) {
            var next = sessions.length > 0 ? sessions[0].id : this.create();
            this.setCurrentId(next);
        }
        return sessions;
    },
    switchTo: function(id) { this.setCurrentId(id); },
    addMessage: function(sessionId, role, content) {
        var sessions = this.getAll();
        var session = null;
        for (var i = 0; i < sessions.length; i++) {
            if (sessions[i].id === sessionId) { session = sessions[i]; break; }
        }
        if (!session) {
            session = { id: sessionId, title: "\u65b0\u4f1a\u8bdd", created: new Date().toISOString(), messages: [], messageCount: 0 };
            sessions.unshift(session);
        }
        session.messages.push({ role: role, content: content, time: new Date().toISOString() });
        session.messageCount = session.messages.length;
        if (role === "user" && session.messageCount <= 2 && session.title === "\u65b0\u4f1a\u8bdd") {
            session.title = content.length > 30 ? content.substring(0, 30) + "..." : content;
        }
        this._save(sessions);
    },
    getMessages: function(sessionId) {
        var sessions = this.getAll();
        for (var i = 0; i < sessions.length; i++) {
            if (sessions[i].id === sessionId) return sessions[i].messages;
        }
        return [];
    }
};

// ============================================================
// State
// ============================================================
var state = {
    sessionId: SessionManager.getCurrentId(),
    skills: [],
    activeSkills: [],
    isProcessing: false,
    tokenEstimate: 0
};

// ============================================================
// Utilities (defined first)
// ============================================================
function escapeHtml(text) {
    if (!text) return "";
    var map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return String(text).replace(/[&<>"']/g, function(c) { return map[c]; });
}

function renderMarkdown(text) {
    if (!text) return "";
    var html = escapeHtml(text);
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function(_, lang, code) {
        return "<pre><code class=\"language-" + lang + "\">" + code.trim() + "</code></pre>";
    });
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
    html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
    html = html.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");
    html = html.replace(/^---+$/gm, "<hr>");
    html = html.replace(/^[\-\*] (.+)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>");
    html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
    html = html.replace(/^\|(.+)\|$/gm, function(match) {
        var cells = match.split("|").filter(function(c) { return c.trim(); });
        if (match.includes("---")) return "";
        return "<tr>" + cells.map(function(c) { return "<td>" + c.trim() + "</td>"; }).join("") + "</tr>";
    });
    html = html.replace(/(<tr>.*<\/tr>\n?)+/g, function(match) {
        var rows = match.match(/<tr>.*?<\/tr>/g) || [];
        if (rows.length >= 2) {
            return "<table><thead>" + rows[0].replace(/td>/g, "th>") + "</thead><tbody>" + rows.slice(1).join("") + "</tbody></table>";
        }
        return "<table>" + match + "</table>";
    });
    html = html.replace(/\n\n+/g, "</p><p>");
    html = "<p>" + html + "</p>";
    html = html.replace(/<p>\s*<\/p>/g, "");
    html = html.replace(/<p>\s*(<h[1-4]>)/g, "$1");
    html = html.replace(/(<\/h[1-4]>)\s*<\/p>/g, "$1");
    html = html.replace(/<p>\s*(<table>)/g, "$1");
    html = html.replace(/(<\/table>)\s*<\/p>/g, "$1");
    html = html.replace(/<p>\s*(<ul>)/g, "$1");
    html = html.replace(/(<\/ul>)\s*<\/p>/g, "$1");
    html = html.replace(/<p>\s*(<blockquote>)/g, "$1");
    html = html.replace(/(<\/blockquote>)\s*<\/p>/g, "$1");
    html = html.replace(/<p>\s*(<pre>)/g, "$1");
    html = html.replace(/(<\/pre>)\s*<\/p>/g, "$1");
    html = html.replace(/<p>\s*(<hr>)\s*<\/p>/g, "$1");
    return html;
}

function scrollToBottom() {
    var cc = document.getElementById("chat-container");
    if (cc) { requestAnimationFrame(function() { cc.scrollTop = cc.scrollHeight; }); }
}

function appendMessage(role, content, isLoading) {
    var cc = document.getElementById("chat-container");
    if (!cc) return null;
    var msgDiv = document.createElement("div");
    msgDiv.className = "message " + role;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    if (isLoading) {
        bubble.innerHTML = "<div class=\"typing-indicator\"><span></span><span></span><span></span></div>";
    } else if (role === "assistant" && content) {
        bubble.innerHTML = renderMarkdown(content);
    } else {
        bubble.textContent = content;
    }
    msgDiv.appendChild(bubble);
    cc.appendChild(msgDiv);
    scrollToBottom();
    return msgDiv;
}

function updateAssistantStatus(msgDiv, statusText) {
    var bubble = msgDiv.querySelector(".bubble");
    if (!bubble) return;
    bubble.innerHTML = "<div class=\"typing-indicator\"><span></span><span></span><span></span></div>"
        + "<div style=\"font-size:12px;color:var(--text-muted);margin-top:4px\">" + escapeHtml(statusText) + "</div>";
}

function updateAssistantContent(msgDiv, content) {
    var bubble = msgDiv.querySelector(".bubble");
    if (!bubble) return;
    bubble.innerHTML = renderMarkdown(content);
    scrollToBottom();
}

function finalizeAssistantMessage(msgDiv, content, inspectionResult, activatedSkills, planTask, analysisPlan) {
    // Extract thinking process for collapsible display (uses unicode bracket markers)
    var thinkingBracket = "\u3010"; // Left bracket 【
    var thinkingMatch = content.match(new RegExp(thinkingBracket + "思考过程" + thinkingBracket + "([\\s\\S]*?)" + thinkingBracket));
    var thinkingContent = thinkingMatch ? thinkingMatch[1].trim() : "";
    if (thinkingContent) {
        content = content.replace(new RegExp(thinkingBracket + "思考过程" + thinkingBracket + "[\\s\\S]*?" + thinkingBracket), thinkingBracket);
    }
    if (analysisPlan && analysisPlan.summary) {
        headerHtml += "<div class=\"analysis-plan-card\">";
        headerHtml += "<div class=\"plan-card-header\">\u5206\u6790\u8ba1\u5212</div>";
        headerHtml += "<div class=\"plan-card-body\">" + escapeHtml(analysisPlan.summary) + "</div>";
        if (analysisPlan.executable_methods && analysisPlan.executable_methods.length) {
            headerHtml += "<div class=\"plan-card-methods\"><span style=\"color:#4ade80\">\u53ef\u6267\u884c: </span>";
            headerHtml += analysisPlan.executable_methods.map(function(m) { return "<code>" + escapeHtml(m.name || m.method_name) + "</code>"; }).join(" ");
            headerHtml += "</div>";
        }
        if (analysisPlan.data_gaps && analysisPlan.data_gaps.length) {
            headerHtml += "<div class=\"plan-card-gaps\"><span style=\"color:#fbbf24\">\u6570\u636e\u7f3a\u53e3: </span>";
            headerHtml += analysisPlan.data_gaps.map(function(g) { return escapeHtml(g.field || g.description); }).join(", ");
            headerHtml += "</div>";
        }
        headerHtml += "</div>";
    }
    if (inspectionResult) {
        var passed = inspectionResult.checks_passed;
        headerHtml += "<div class=\"inspection-badge " + (passed ? "passed" : "warning") + "\">"
            + (passed ? "\u2705" : "\u26a0\ufe0f") + " \u6570\u636e\u6838\u67e5: " + escapeHtml(inspectionResult.summary || "")
            + "</div>";
    }
    if (activatedSkills && activatedSkills.length) {
        headerHtml += "<div style=\"font-size:11px;color:var(--text-muted);margin-bottom:8px\">"
            + "\ud83e\udde0 \u5df2\u6fc0\u6d3b: " + activatedSkills.map(function(s) { return "<code>" + s + "</code>"; }).join(" ")
            + "</div>";
    }
    var bubble = msgDiv.querySelector(".bubble");
    if (!bubble) return;
    bubble.innerHTML = headerHtml + renderMarkdown(content);
    scrollToBottom();
}

function detectAndRenderChart(container) {
    var tables = container.querySelectorAll("table");
    for (var t = 0; t < tables.length; t++) {
        var table = tables[t];
        if (table.closest(".chart-processed")) continue;
        table.classList.add("chart-processed");
        var rows = table.querySelectorAll("tr");
        if (rows.length < 2) continue;
        var headers = [];
        var hcells = rows[0].querySelectorAll("th,td");
        for (var h = 0; h < hcells.length; h++) { headers.push(hcells[h].textContent.trim()); }
        var dateCol = -1;
        for (var i = 0; i < headers.length; i++) {
            var h = headers[i].toLowerCase();
            if (/(?:date|day|time|\u65e5\u671f|\u65f6\u95f4|\u5929)/.test(h)) { dateCol = i; break; }
        }
        if (dateCol < 0) continue;
        var labels = [];
        var datasets = [];
        var numCols = [];
        for (var j = 0; j < headers.length; j++) {
            if (j === dateCol) continue;
            var isNumeric = true;
            for (var k = 1; k < Math.min(rows.length, 5); k++) {
                var td = rows[k].querySelectorAll("td")[j];
                if (td && isNaN(parseFloat(td.textContent.replace(/,/g, "")))) { isNumeric = false; break; }
            }
            if (isNumeric) numCols.push(j);
        }
        if (numCols.length === 0) continue;
        for (var r = 1; r < rows.length; r++) {
            var cells = rows[r].querySelectorAll("td");
            if (cells.length <= dateCol) continue;
            labels.push(cells[dateCol].textContent.trim().substring(0, 10));
        }
        var colors = ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#ec4899"];
        for (var c = 0; c < numCols.length; c++) {
            var colIdx = numCols[c];
            var data = [];
            for (var r2 = 1; r2 < rows.length; r2++) {
                var cells2 = rows[r2].querySelectorAll("td");
                data.push(cells2.length > colIdx ? (parseFloat(cells2[colIdx].textContent.replace(/,/g, "")) || 0) : 0);
            }
            datasets.push({
                label: headers[colIdx],
                data: data,
                borderColor: colors[c % colors.length],
                backgroundColor: colors[c % colors.length] + "20",
                tension: 0.3,
                fill: false
            });
        }
        if (labels.length === 0 || datasets.length === 0) continue;
        var chartDiv = document.createElement("div");
        chartDiv.className = "chart-container";
        var canvas = document.createElement("canvas");
        canvas.id = "chart_" + Date.now() + "_" + t;
        chartDiv.appendChild(canvas);
        table.parentNode.insertBefore(chartDiv, table.nextSibling);
        if (typeof Chart !== "undefined") {
            new Chart(canvas, {
                type: labels.length <= 3 ? "bar" : "line",
                data: { labels: labels, datasets: datasets },
                options: {
                    responsive: true,
                    plugins: { legend: { labels: { color: "#94a3b8", font: { size: 11 } } } },
                    scales: {
                        x: { ticks: { color: "#64748b", font: { size: 10 }, maxRotation: 45 } },
                        y: { ticks: { color: "#64748b", font: { size: 10 } }, grid: { color: "#1e293b" } }
                    }
                }
            });
        }
    }
}

// ============================================================
// Skills rendering
// ============================================================
function renderSkillsEmpty() {
    var list = document.getElementById("skills-list");
    if (!list) return;
    list.innerHTML = "<div class=\"activated-skills-empty\">"
        + "<div class=\"empty-icon\">\ud83d\udd0d</div>"
        + "<div>\u5206\u6790\u65b9\u6cd5\u4f1a\u5728\u5206\u6790\u8fc7\u7a0b\u4e2d\u81ea\u52a8\u663e\u793a</div>"
        + "</div>";
}

function showActivatedSkills(skillNames) {
    var sidebar = document.getElementById("sidebar");
    var list = document.getElementById("skills-list");
    if (!sidebar || !list) return;
    if (!skillNames || !skillNames.length) {
        renderSkillsEmpty();
        sidebar.classList.remove("visible");
        return;
    }
    var html = "";
    var catLabels = { "statistical": "Statistical", "testing": "Experiment", "ml": "ML" };
    for (var i = 0; i < skillNames.length; i++) {
        var name = skillNames[i];
        var cat = "other";
        for (var j = 0; j < state.skills.length; j++) {
            if (state.skills[j].name === name) { cat = state.skills[j].category || "other"; break; }
        }
        html += "<div class=\"skill-category\">";
        html += "<div class=\"skill-cat-header\">" + (catLabels[cat] || cat) + "</div>";
        html += "<div class=\"skill-item active\" data-name=\"" + name + "\">";
        html += "<span class=\"skill-name\">" + name + "</span>";
        html += "</div>";
        html += "</div>";
    }
    list.innerHTML = html;
    sidebar.classList.add("visible");
}

// ============================================================
// Session rendering
// ============================================================
function renderSessionTokenBar(sessionId) {
    var footer = document.getElementById("session-token-display");
    if (!footer) return;
    var msgs = SessionManager.getMessages(sessionId);
    var totalText = "";
    for (var i = 0; i < msgs.length; i++) {
        totalText += (msgs[i].content || "") + " ";
    }
    var estTokens = totalText.length * 0.5; // rough estimate
    var maxTokens = 128000;
    var pct = Math.min(100, Math.round((estTokens / maxTokens) * 100));
    var cls = pct > 80 ? "danger" : (pct > 60 ? "warning" : "");
    footer.innerHTML = '<span class="bar-label">上下文: ' + pct + '%</span>' +
        '<div class="token-fill"><div class="token-fill-inner" style="width:' + pct + '%"></div></div>';
    if (cls) footer.className = "session-token-bar " + cls;
    else footer.className = "session-token-bar";
}
function renderSessions() {
    var list = document.getElementById("session-list");
    var counter = document.getElementById("session-count");
    if (!list) return;
    var sessions = SessionManager.getAll();
    var currentId = SessionManager.getCurrentId();
    if (counter) counter.textContent = sessions.length + " \u4e2a\u4f1a\u8bdd";
    list.innerHTML = "";
    for (var i = 0; i < sessions.length; i++) {
        (function(s) {
            var item = document.createElement("div");
            item.className = "session-item" + (s.id === currentId ? " active" : "");
            item.setAttribute("data-session-id", s.id);
            item.innerHTML = "<span class=\"session-item-title\">" + escapeHtml(s.title || "\u65b0\u4f1a\u8bdd") + "</span>"
                + "<button class=\"session-item-del\" title=\"\u5220\u9664\">&times;</button>";
            item.querySelector(".session-item-title").onclick = function(e) { e.stopPropagation(); switchToSession(s.id); };
            item.querySelector(".session-item-del").onclick = function(e) { e.stopPropagation(); deleteSession(s.id); };
            list.appendChild(item);
        })(sessions[i]);
    }
    renderSessionTokenBar(currentId);
}

function loadCurrentSessionMessages() {
    var messages = SessionManager.getMessages(state.sessionId);
    if (messages.length === 0) return;
    for (var i = 0; i < messages.length; i++) {
        var msg = messages[i];
        if (msg.role === "user") {
            appendMessage("user", msg.content);
        } else if (msg.role === "assistant") {
            var d = appendMessage("assistant", "", false);
            if (d) {
                var b = d.querySelector(".bubble");
                if (b) b.innerHTML = renderMarkdown(msg.content);
            }
        }
    }
}

function switchToSession(id) {
    if (state.isProcessing) return;
    SessionManager.switchTo(id);
    state.sessionId = id;
    state.activeSkills = [];
    showActivatedSkills([]);
    var hint = document.getElementById("active-skills-hint");
    if (hint) hint.textContent = "";
    var cc = document.getElementById("chat-container");
    if (cc) { cc.innerHTML = ""; }
    loadCurrentSessionMessages();
    renderSessions();
    updateTokenIndicator(0);
    renderSessionTokenBar(state.sessionId);
    var qi = document.getElementById("query-input");
    if (qi) qi.focus();
}

function deleteSession(id) {
    SessionManager.delete(id);
    if (state.sessionId === id) {
        state.sessionId = SessionManager.getCurrentId();
        switchToSession(state.sessionId);
    }
    renderSessions();
}

function estimateTokens() {
    // Rough estimate: 1 token ~= 3 chars for Chinese, 4 chars for English
    var total = 0;
    var messages = SessionManager.getMessages(state.sessionId);
    for (var i = 0; i < messages.length; i++) {
        total += messages[i].content.length;
    }
    // Heuristic: ~2.5 chars per token for mixed content
    return Math.round(total / 2.5);
}

function updateTokenIndicator(tokenCount) {
    var el = document.getElementById("token-usage");
    if (!el) return;
    if (!tokenCount || tokenCount <= 0) {
        tokenCount = estimateTokens();
    }
    var modelMax = 128000;
    var pct = tokenCount > 0 ? Math.round(tokenCount / modelMax * 100) : 0;
    el.textContent = "u{1f4ca} " + (pct > 0 ? pct + "% (" + tokenCount.toLocaleString() + ")" : "--");
    el.title = "u5f53u524du4f1au8bddu4e0au4e0bu6587: " + tokenCount.toLocaleString() + " tokens / " + modelMax.toLocaleString();
    el.className = "token-indicator"
        + (pct >= 80 ? " danger" : "")
        + (pct >= 50 && pct < 80 ? " warning" : "");
}

function autoResizeTextarea() {
    var qi = document.getElementById("query-input");
    if (!qi) return;
    qi.style.height = "auto";
    qi.style.height = Math.min(qi.scrollHeight, 100) + "px";
}

// ============================================================
// Send message
// ============================================================
async function sendMessage() {
    var qi = document.getElementById("query-input");
    var sb = document.getElementById("send-btn");
    if (!qi || !sb || state.isProcessing) return;
    var query = qi.value.trim();
    if (!query) return;

    state.isProcessing = true;
    sb.disabled = true;
    qi.value = "";
    qi.style.height = "auto";

    appendMessage("user", query);
    SessionManager.addMessage(state.sessionId, "user", query);
    renderSessions();

    var assistantMsg = appendMessage("assistant", "", true);
    if (!assistantMsg) { state.isProcessing = false; sb.disabled = false; return; }

    try {
        var response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: query, session_id: state.sessionId })
        });
        if (!response.ok) throw new Error("HTTP " + response.status);

        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var fullContent = "";
        var inspectionResult = null;
        var activatedSkills = [];
        var planTask = null;
        var analysisPlan = null;

        while (true) {
            var result = await reader.read();
            if (result.done) break;
            buffer += decoder.decode(result.value, { stream: true });
            var events = buffer.split("\n\n");
            buffer = events.pop() || "";

            for (var ei = 0; ei < events.length; ei++) {
                var block = events[ei].trim();
                if (!block) continue;
                var lines_ = block.split("\n");
                var eventType = "";
                var eventData = "";
                for (var li = 0; li < lines_.length; li++) {
                    if (lines_[li].startsWith("event: ")) eventType = lines_[li].slice(7).trim();
                    else if (lines_[li].startsWith("data: ")) eventData = lines_[li].slice(6).trim();
                }
                if (!eventData) continue;
                try {
                    var data = JSON.parse(eventData);
                    switch (eventType) {
                        case "status":
                            updateAssistantStatus(assistantMsg, data.msg || data.phase);
                            break;
                        case "token":
                            fullContent += data.content || "";
                            updateAssistantContent(assistantMsg, fullContent);
                            break;
                        case "metadata":
                            inspectionResult = data.inspection_result;
                            activatedSkills = data.activated_skills || [];
                            planTask = data.plan_task || null;
                            analysisPlan = data.analysis_plan || null;
                            state.activeSkills = activatedSkills;
                            showActivatedSkills(activatedSkills);
                            var hint = document.getElementById("active-skills-hint");
                            if (hint && activatedSkills.length) {
                                hint.textContent = "\u5df2\u6fc0\u6d3b: " + activatedSkills.join(", ");
                            }
                            if (data.token_usage) updateTokenIndicator(data.token_usage);
                            break;
                        case "done":
                            finalizeAssistantMessage(assistantMsg, fullContent, inspectionResult, activatedSkills, planTask, analysisPlan);
                            if (fullContent) {
                                SessionManager.addMessage(state.sessionId, "assistant", fullContent);
                                renderSessions();
    updateTokenIndicator(0);
    renderSessionTokenBar(state.sessionId);
                            }
                            break;
                    }
                } catch (parseErr) {}
            }
        }

        if (buffer.trim()) {
            var remLines = buffer.split("\n");
            for (var rl = 0; rl < remLines.length; rl++) {
                if (remLines[rl].startsWith("data: ")) {
                    try {
                        var d = JSON.parse(remLines[rl].slice(6));
                        if (d.content) { fullContent += d.content; updateAssistantContent(assistantMsg, fullContent); }
                    } catch(e) {}
                }
            }
        }

        if (fullContent) {
            updateAssistantContent(assistantMsg, fullContent);
            SessionManager.addMessage(state.sessionId, "assistant", fullContent);
            renderSessions();
    updateTokenIndicator(0);
    renderSessionTokenBar(state.sessionId);
        } else {
            updateAssistantContent(assistantMsg, "\u5206\u6790\u5b8c\u6210\uff0c\u4f46\u672a\u6536\u5230\u6709\u6548\u56de\u590d\u3002\u8bf7\u91cd\u8bd5\u3002");
        }
    } catch (error) {
        console.error("\u804a\u5929\u8bf7\u6c42\u5931\u8d25:", error);
        updateAssistantContent(assistantMsg, "\u26a0\ufe0f \u8bf7\u6c42\u5931\u8d25: " + error.message + "\n\n\u8bf7\u786e\u8ba4\u670d\u52a1\u662f\u5426\u6b63\u5e38\u8fd0\u884c\u3002");
    } finally {
        state.isProcessing = false;
        sb.disabled = false;
        if (qi) qi.focus();
        scrollToBottom();
        setTimeout(function() { detectAndRenderChart(assistantMsg); }, 150);
    }
}

// ============================================================
// Event wiring
// ============================================================

// ============================================================
// Database Scanner
// ============================================================
async function scanDatabase() {
    var btn = document.getElementById("scan-db-btn");
    var statusDiv = document.getElementById("scan-status");
    var statusText = document.getElementById("scan-status-text");
    if (!btn || !statusDiv || !statusText) return;
    if (btn.disabled) return;
    
    btn.disabled = true;
    btn.classList.add("scanning");
    btn.textContent = "⏳ 扫描中...";
    statusDiv.style.display = "block";
    statusText.textContent = "正在连接数据库...";
    
    try {
        var response = await fetch("/api/scan", { method: "POST" });
        if (!response.ok) throw new Error("HTTP " + response.status);
        
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        
        while (true) {
            var result = await reader.read();
            if (result.done) break;
            buffer += decoder.decode(result.value, { stream: true });
            var events = buffer.split("\n\n");
            buffer = events.pop() || "";
            
            for (var i = 0; i < events.length; i++) {
                var block = events[i].trim();
                if (!block) continue;
                var lines = block.split("\n");
                var eventType = "", eventData = "";
                for (var j = 0; j < lines.length; j++) {
                    if (lines[j].startsWith("event: ")) eventType = lines[j].slice(7).trim();
                    else if (lines[j].startsWith("data: ")) eventData = lines[j].slice(6).trim();
                }
                if (!eventData) continue;
                try {
                    var data = JSON.parse(eventData);
                    if (eventType === "status") {
                        statusText.textContent = data.msg || data.phase;
                    } else if (eventType === "done") {
                        statusText.textContent = data.msg;
                        statusDiv.style.borderColor = "rgba(52,211,153,0.5)";
                        setTimeout(function() { statusDiv.style.display = "none"; }, 5000);
                    } else if (eventType === "error") {
                        statusText.textContent = "扫描失败: " + (data.error || "未知错误");
                        statusDiv.style.color = "var(--error)";
                    }
                } catch(e) {}
            }
        }
    } catch (error) {
        statusText.textContent = "扫描失败: " + error.message;
        console.error("Scan failed:", error);
    } finally {
        btn.disabled = false;
        btn.classList.remove("scanning");
        btn.textContent = "🔍 扫描";
    }
}

function setupEventListeners() {
    var sb = document.getElementById("send-btn");
    var qi = document.getElementById("query-input");
    var nsb = document.getElementById("new-session-btn");
    var nchb = document.getElementById("new-chat-header-btn");
    var st = document.getElementById("session-toggle");
    var ss = document.getElementById("session-sidebar");

    if (sb) sb.addEventListener("click", sendMessage);
    if (qi) {
        qi.addEventListener("keydown", function(e) {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
        });
        qi.addEventListener("input", autoResizeTextarea);
    }
    if (nsb) {
        nsb.addEventListener("click", function() {
            var newId = SessionManager.create();
            switchToSession(newId);
        });
    }
    if (nchb) {
        nchb.addEventListener("click", function() {
            var newId = SessionManager.create();
            switchToSession(newId);
        });
    }

    var scanBtn = document.getElementById("scan-db-btn");
    if (scanBtn) scanBtn.addEventListener("click", scanDatabase);

    if (st && ss) {
        st.addEventListener("click", function() { ss.classList.toggle("collapsed"); });
    }

    // Quick tags
    var cc = document.getElementById("chat-container");
    if (cc) {
        cc.addEventListener("click", function(e) {
            var tag = e.target.closest(".quick-tag");
            if (tag && qi) { qi.value = tag.dataset.query; sendMessage(); }
        });
    }

    // Feature tabs
    var ft = document.getElementById("feature-tabs");
    if (ft) {
        ft.addEventListener("click", function(e) {
            var tab = e.target.closest(".feature-tab");
            if (!tab) return;
            var tabs = ft.querySelectorAll(".feature-tab");
            for (var ti = 0; ti < tabs.length; ti++) { tabs[ti].classList.remove("active"); }
            tab.classList.add("active");
        });
    }
}

// ============================================================
// Init
// ============================================================
async function init() {
    // Safety: reset processing state on load (handles browser refresh during processing)
    state.isProcessing = false;
    var sb2 = document.getElementById("send-btn");
    if (sb2) sb2.disabled = false;
    try {
        state.sessionId = SessionManager.getCurrentId();
        renderSkillsEmpty();
        // Load skills catalog
        try {
            var skillsResp = await fetch("/api/skills");
            if (skillsResp.ok) {
                var skillsData = await skillsResp.json();
                state.skills = skillsData.skills || [];
            }
        } catch(e) { console.warn("Skills load failed:", e); }
        setupEventListeners();
        renderSessions();
        loadCurrentSessionMessages();
        updateTokenIndicator(0);
        autoResizeTextarea();
    } catch(e) {
        console.error("Init failed:", e);
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
