/**
 * RuriSkry — Executive Deck Builder
 *
 * Builds an editable PPTX for CTO / tech-leader / sales-leader audiences.
 * Follows the Anthropic PPTX skill design rules:
 *   - Bold content-informed palette (garnet/parchment/brass — NOT default blue)
 *   - One color dominates visually; committed visual motif on every slide
 *   - No accent lines under titles (AI tell)
 *   - Left-aligned body; varied layouts across slides
 *   - Every slide carries at least one visual element (icon, shape, or card)
 *
 * Run:
 *   node scripts/deck/build-exec-deck.js
 * Output:
 *   docs/RuriSkry-Exec-Deck.pptx
 */

const path = require("path");
const fs = require("fs");
const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const {
  FaExclamationTriangle,
  FaShieldAlt,
  FaBalanceScale,
  FaUsersCog,
  FaBomb,
  FaBook,
  FaHistory,
  FaChartPie,
  FaCheckCircle,
  FaHourglassHalf,
  FaTimesCircle,
  FaCloud,
  FaFileAlt,
  FaBolt,
  FaPiggyBank,
  FaClipboardList,
  FaGithub,
  FaRoad,
  FaCogs,
  FaSearchDollar,
  FaServer,
  FaNetworkWired,
  FaGavel,
  FaLayerGroup,
  FaEye,
  FaArrowRight,
} = require("react-icons/fa");

// -------------------------------------------------------------
// Palette — "Charcoal Minimal" (charcoal / off-white / amber)
// Modern SaaS aesthetic — Linear / Vercel / Stripe docs. Reads as serious
// engineering rather than consulting. Amber accent signals "attention" without
// screaming. Variable names kept for diff minimalism.
// -------------------------------------------------------------
const C = {
  garnet: "2F3E46",    // Dominant: charcoal — hero backgrounds, section headers, motif
  garnetDeep: "1F2A30", // Deeper charcoal for contrast under amber
  parchment: "F7F7F5",  // Body slide background (off-white)
  parchmentDeep: "E9E9E4", // Subtle card surfaces on body
  brass: "F59E0B",      // Accent: amber — stamps, markers, callouts
  walnut: "1F2937",     // Primary body text (slate-900)
  mutedBrown: "64748B", // Metadata, captions (slate-500)
  ink: "0F172A",        // High-contrast body alt (slate-950)
  // Verdict coding (used only on decision-surface slide)
  approved: "3F6B4E",
  escalated: "B88A2E",
  denied: "8B2635",
  white: "FFFFFF",
};

// -------------------------------------------------------------
// Icon rasterisation helpers
// -------------------------------------------------------------
async function iconPng(IconComponent, color = "#" + C.garnet, size = 256) {
  const svg = ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(size) })
  );
  const png = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + png.toString("base64");
}

// -------------------------------------------------------------
// Layout constants (inches — LAYOUT_WIDE is 13.333 x 7.5)
// -------------------------------------------------------------
const SW = 13.333;  // slide width
const SH = 7.5;     // slide height
const MARGIN = 0.6;
const TITLE_Y = 0.55;
const TITLE_H = 0.9;
const CONTENT_Y = 1.65;
const FONT_HEAD = "Georgia";
const FONT_BODY = "Calibri";

// -------------------------------------------------------------
// Slide helpers
// -------------------------------------------------------------
function addMotif(slide) {
  // Garnet vertical bracket to the left of every content slide title.
  // 0.12" wide — sits flush with left margin.
  slide.addShape("rect", {
    x: MARGIN, y: TITLE_Y + 0.08,
    w: 0.11, h: TITLE_H - 0.16,
    fill: { color: C.garnet },
    line: { color: C.garnet, width: 0 },
  });
}

function addFooter(slide, pageNum, totalPages) {
  // Subtle brass page number bottom-right, muted brown tag bottom-left.
  slide.addText("RuriSkry · Confidential", {
    x: MARGIN, y: SH - 0.45, w: 5, h: 0.25,
    fontFace: FONT_BODY, fontSize: 10, color: C.mutedBrown,
    align: "left", valign: "middle",
  });
  slide.addText(`${pageNum}`, {
    x: SW - MARGIN - 0.5, y: SH - 0.45, w: 0.5, h: 0.25,
    fontFace: FONT_BODY, fontSize: 10, color: C.brass, bold: true,
    align: "right", valign: "middle",
  });
}

function addTitle(slide, title) {
  slide.addText(title, {
    x: MARGIN + 0.3, y: TITLE_Y, w: SW - 2 * MARGIN - 0.3, h: TITLE_H,
    fontFace: FONT_HEAD, fontSize: 36, bold: true, color: C.walnut,
    align: "left", valign: "middle", margin: 0,
  });
}

function contentSlideBase(pres, pageNum, totalPages, title) {
  const s = pres.addSlide();
  s.background = { color: C.parchment };
  addMotif(s);
  addTitle(s, title);
  addFooter(s, pageNum, totalPages);
  return s;
}

// -------------------------------------------------------------
// Main builder
// -------------------------------------------------------------
(async () => {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE"; // 13.333 x 7.5
  pres.author = "RuriSkry";
  pres.title = "RuriSkry — An AI Change Advisory Board for Autonomous Cloud Agents";
  pres.company = "RuriSkry";
  pres.subject = "Executive briefing";

  // -----------------------------------------------------------
  // Pre-render all icons (parallel)
  // -----------------------------------------------------------
  const [
    icnWarning, icnShield, icnScale, icnCab,
    icnBomb, icnBook, icnHistory, icnChart,
    icnCheck, icnHourglass, icnX,
    icnCloud, icnFile, icnBolt, icnPiggy, icnClipboard,
    icnGithub, icnRoad, icnCogs, icnSearchDollar,
    icnServer, icnNetwork, icnGavel, icnLayers, icnEye, icnArrow,
    icnShieldCream, icnScaleCream,
  ] = await Promise.all([
    iconPng(FaExclamationTriangle, "#" + C.garnet),
    iconPng(FaShieldAlt, "#" + C.garnet),
    iconPng(FaBalanceScale, "#" + C.garnet),
    iconPng(FaUsersCog, "#" + C.garnet),
    iconPng(FaBomb, "#" + C.garnet),
    iconPng(FaBook, "#" + C.garnet),
    iconPng(FaHistory, "#" + C.garnet),
    iconPng(FaChartPie, "#" + C.garnet),
    iconPng(FaCheckCircle, "#" + C.approved),
    iconPng(FaHourglassHalf, "#" + C.escalated),
    iconPng(FaTimesCircle, "#" + C.denied),
    iconPng(FaCloud, "#" + C.garnet),
    iconPng(FaFileAlt, "#" + C.garnet),
    iconPng(FaBolt, "#" + C.garnet),
    iconPng(FaPiggyBank, "#" + C.garnet),
    iconPng(FaClipboardList, "#" + C.garnet),
    iconPng(FaGithub, "#" + C.garnet),
    iconPng(FaRoad, "#" + C.garnet),
    iconPng(FaCogs, "#" + C.garnet),
    iconPng(FaSearchDollar, "#" + C.garnet),
    iconPng(FaServer, "#" + C.garnet),
    iconPng(FaNetworkWired, "#" + C.garnet),
    iconPng(FaGavel, "#" + C.brass),
    iconPng(FaLayerGroup, "#" + C.garnet),
    iconPng(FaEye, "#" + C.garnet),
    iconPng(FaArrowRight, "#" + C.brass),
    iconPng(FaShieldAlt, "#" + C.parchment),
    iconPng(FaBalanceScale, "#" + C.parchment),
  ]);

  const TOTAL = 15;

  // =============================================================
  // SLIDE 1 — Title (garnet hero)
  // =============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.garnet };

    // Brass gavel icon centered above title
    s.addImage({ data: icnGavel, x: SW / 2 - 0.5, y: 1.8, w: 1.0, h: 1.0 });

    s.addText("RuriSkry", {
      x: 0, y: 3.0, w: SW, h: 1.0,
      fontFace: FONT_HEAD, fontSize: 72, italic: true, bold: true,
      color: C.white, align: "center",
    });

    s.addText("An AI Change Advisory Board\nfor Autonomous Cloud Agents", {
      x: 0, y: 4.2, w: SW, h: 1.2,
      fontFace: FONT_HEAD, fontSize: 26,
      color: C.brass, align: "center",
    });

    s.addText("Azure-native · Open source · Production-grade", {
      x: 0, y: 6.2, w: SW, h: 0.3,
      fontFace: FONT_BODY, fontSize: 14,
      color: "CBD5E1", align: "center", italic: true,
    });
  }

  // =============================================================
  // SLIDE 2 — The emerging risk (3 icon cards)
  // =============================================================
  {
    const s = contentSlideBase(pres, 2, TOTAL, "The emerging risk");

    // Intro line
    s.addText(
      "Autonomous AI agents are now making infrastructure decisions at enterprise scale — with only capability controls (IAM, scopes) and no judgment layer.",
      {
        x: MARGIN, y: 1.75, w: SW - 2 * MARGIN, h: 0.8,
        fontFace: FONT_BODY, fontSize: 16, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      }
    );

    // 3 cards: Cost agent, SRE agent, Deployment agent
    const cardY = 3.0, cardH = 2.6, cardW = 4.0, gap = 0.25;
    const startX = (SW - (cardW * 3 + gap * 2)) / 2;
    const cards = [
      {
        icon: icnPiggy,
        title: "Cost agent",
        body: "Deletes a DR VM to save $847/mo — without seeing the compliance tag that made it untouchable.",
      },
      {
        icon: icnBolt,
        title: "SRE agent",
        body: "Restarts a payment service — unaware that identical restarts triggered cascade failures three times before.",
      },
      {
        icon: icnNetwork,
        title: "Deployment agent",
        body: "Opens a network port — inadvertently exposing internal admin dashboards to the public internet.",
      },
    ];
    cards.forEach((card, i) => {
      const x = startX + i * (cardW + gap);
      // Card surface
      s.addShape("rect", {
        x, y: cardY, w: cardW, h: cardH,
        fill: { color: C.parchmentDeep },
        line: { color: C.parchmentDeep, width: 0 },
      });
      // Top-left garnet corner accent
      s.addShape("rect", {
        x, y: cardY, w: 0.08, h: cardH,
        fill: { color: C.garnet }, line: { color: C.garnet, width: 0 },
      });
      // Icon
      s.addImage({ data: card.icon, x: x + 0.35, y: cardY + 0.35, w: 0.6, h: 0.6 });
      // Title
      s.addText(card.title, {
        x: x + 0.35, y: cardY + 1.05, w: cardW - 0.7, h: 0.5,
        fontFace: FONT_HEAD, fontSize: 20, bold: true, color: C.walnut,
        align: "left", margin: 0,
      });
      // Body
      s.addText(card.body, {
        x: x + 0.35, y: cardY + 1.6, w: cardW - 0.7, h: cardH - 1.75,
        fontFace: FONT_BODY, fontSize: 14, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      });
    });

    // Bottom line
    s.addText(
      "Nobody today asks the judgment question before the agent acts.",
      {
        x: MARGIN, y: 6.0, w: SW - 2 * MARGIN, h: 0.4,
        fontFace: FONT_HEAD, fontSize: 18, italic: true, color: C.garnet,
        align: "center", margin: 0,
      }
    );
  }

  // =============================================================
  // SLIDE 3 — Guardrails are not governance (2-col comparison)
  // =============================================================
  {
    const s = contentSlideBase(pres, 3, TOTAL, "Guardrails are not governance");

    const colY = 1.9, colH = 4.4, colW = 5.7, gap = 0.4;
    const startX = (SW - (colW * 2 + gap)) / 2;

    // LEFT — Guardrails
    const x1 = startX;
    s.addShape("rect", {
      x: x1, y: colY, w: colW, h: colH,
      fill: { color: C.parchmentDeep },
      line: { color: C.parchmentDeep, width: 0 },
    });
    s.addImage({ data: icnShield, x: x1 + 0.4, y: colY + 0.35, w: 0.7, h: 0.7 });
    s.addText("Guardrails", {
      x: x1 + 0.4, y: colY + 1.15, w: colW - 0.8, h: 0.5,
      fontFace: FONT_HEAD, fontSize: 24, bold: true, color: C.walnut, margin: 0,
    });
    s.addText('Can the agent do this?', {
      x: x1 + 0.4, y: colY + 1.75, w: colW - 0.8, h: 0.4,
      fontFace: FONT_HEAD, fontSize: 16, italic: true, color: C.garnet, margin: 0,
    });
    s.addText([
      { text: "IAM roles, token scopes, deny-lists, rate limits.", options: { breakLine: true } },
      { text: " " , options: { breakLine: true, fontSize: 4 } },
      { text: "Every major cloud ships them.", options: {} },
    ], {
      x: x1 + 0.4, y: colY + 2.35, w: colW - 0.8, h: colH - 2.5,
      fontFace: FONT_BODY, fontSize: 15, color: C.walnut,
      align: "left", valign: "top", margin: 0,
    });

    // RIGHT — Governance
    const x2 = startX + colW + gap;
    s.addShape("rect", {
      x: x2, y: colY, w: colW, h: colH,
      fill: { color: C.garnet },
      line: { color: C.garnet, width: 0 },
    });
    s.addImage({ data: icnScaleCream, x: x2 + 0.4, y: colY + 0.35, w: 0.7, h: 0.7 });
    s.addText("Governance", {
      x: x2 + 0.4, y: colY + 1.15, w: colW - 0.8, h: 0.5,
      fontFace: FONT_HEAD, fontSize: 24, bold: true, color: C.white, margin: 0,
    });
    s.addText("Should the agent do this — right now, on this resource?", {
      x: x2 + 0.4, y: colY + 1.75, w: colW - 0.8, h: 0.4,
      fontFace: FONT_HEAD, fontSize: 16, italic: true, color: C.brass, margin: 0,
    });
    s.addText([
      { text: "Blast radius. Policy fit. Precedent. Cost. Evidence quality.", options: { breakLine: true } },
      { text: " ", options: { breakLine: true, fontSize: 4 } },
      { text: "Almost nobody ships them.", options: {} },
    ], {
      x: x2 + 0.4, y: colY + 2.35, w: colW - 0.8, h: colH - 2.5,
      fontFace: FONT_BODY, fontSize: 15, color: C.white,
      align: "left", valign: "top", margin: 0,
    });

    s.addText("Most AI-agent tooling stops at guardrails. The judgment layer is missing.", {
      x: MARGIN, y: 6.5, w: SW - 2 * MARGIN, h: 0.35,
      fontFace: FONT_HEAD, fontSize: 16, italic: true, color: C.garnet,
      align: "center", margin: 0,
    });
  }

  // =============================================================
  // SLIDE 4 — The pattern that already works (icon list)
  // =============================================================
  {
    const s = contentSlideBase(pres, 4, TOTAL, "The pattern that already works");

    s.addText(
      "Every mature enterprise routes production change through a Change Advisory Board. A senior reviewer checks:",
      {
        x: MARGIN, y: 1.75, w: SW - 2 * MARGIN, h: 0.5,
        fontFace: FONT_BODY, fontSize: 16, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      }
    );

    // 5 horizontal icon rows
    const rows = [
      { icon: icnBomb, title: "Blast radius", body: "What breaks if this goes wrong?" },
      { icon: icnBook, title: "Policy", body: "Does this violate compliance or internal standard?" },
      { icon: icnHistory, title: "Precedent", body: "Has this pattern failed before?" },
      { icon: icnChart, title: "Cost", body: "Is this change financially sane?" },
      { icon: icnClipboard, title: "Conditions", body: "Maintenance window, canary rollout, rollback pre-staged." },
    ];
    const rowY = 2.55, rowH = 0.65, rowGap = 0.08;
    rows.forEach((r, i) => {
      const y = rowY + i * (rowH + rowGap);
      s.addImage({ data: r.icon, x: MARGIN + 0.3, y: y + 0.1, w: 0.5, h: 0.5 });
      s.addText(r.title, {
        x: MARGIN + 1.0, y: y, w: 2.8, h: rowH,
        fontFace: FONT_HEAD, fontSize: 18, bold: true, color: C.garnet,
        align: "left", valign: "middle", margin: 0,
      });
      s.addText(r.body, {
        x: MARGIN + 3.9, y: y, w: SW - MARGIN - 3.9 - MARGIN, h: rowH,
        fontFace: FONT_BODY, fontSize: 15, color: C.walnut,
        align: "left", valign: "middle", margin: 0,
      });
    });

    s.addText(
      "What if every AI-agent action went through the same review — automatically, in seconds, before reaching Azure?",
      {
        x: MARGIN, y: 6.4, w: SW - 2 * MARGIN, h: 0.45,
        fontFace: FONT_HEAD, fontSize: 16, italic: true, color: C.garnet,
        align: "center", margin: 0,
      }
    );
  }

  // =============================================================
  // SLIDE 5 — Tagline hero (second garnet slide)
  // =============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.garnet };

    s.addText("RuriSkry", {
      x: 0, y: 1.8, w: SW, h: 0.9,
      fontFace: FONT_HEAD, fontSize: 44, italic: true, color: C.brass, align: "center",
    });

    s.addText("AI agents propose the fix.", {
      x: 0, y: 3.1, w: SW, h: 1.0,
      fontFace: FONT_HEAD, fontSize: 54, bold: true, color: C.white, align: "center",
    });
    s.addText("An AI Change Advisory Board decides if it ships.", {
      x: 0, y: 4.25, w: SW, h: 1.0,
      fontFace: FONT_HEAD, fontSize: 40, color: C.white, align: "center",
    });

    // Brass divider dot
    s.addShape("ellipse", {
      x: SW / 2 - 0.08, y: 5.7, w: 0.16, h: 0.16,
      fill: { color: C.brass }, line: { color: C.brass, width: 0 },
    });
  }

  // =============================================================
  // SLIDE 6 — Why this matters (business value, paired with pain)
  // =============================================================
  {
    const s = contentSlideBase(pres, 6, TOTAL, "Why this matters to the business");

    // Pre-render parchment-coloured icon variants so they pop on charcoal circles.
    const [oShield, oFile, oBolt, oPiggy, oClipboard] = await Promise.all([
      iconPng(FaShieldAlt, "#" + C.parchment),
      iconPng(FaFileAlt, "#" + C.parchment),
      iconPng(FaBolt, "#" + C.parchment),
      iconPng(FaPiggyBank, "#" + C.parchment),
      iconPng(FaClipboardList, "#" + C.parchment),
    ]);

    const outcomes = [
      { icon: oShield, title: "Containment of AI-driven incidents", body: "Every agent action pre-scored before it reaches Azure." },
      { icon: oFile, title: "Audit-ready on day one", body: "Cosmos DB lineage — every decision reconstructable from inputs." },
      { icon: oBolt, title: "Faster incident response", body: "Azure Monitor alerts route through the same pipeline; LLM-verified remediation plan in minutes, rollback pre-staged." },
      { icon: oPiggy, title: "Cost discipline", body: "Cost agent surfaces waste; governance ensures DR and compliance tags aren't stripped by over-optimisation." },
      { icon: oClipboard, title: "Compliance evidence", body: "Policy engine + full lineage supports SOC 2, ISO 27001, and PCI change-control obligations." },
    ];
    const rowY = 1.9, rowH = 0.9, rowGap = 0.1;
    outcomes.forEach((o, i) => {
      const y = rowY + i * (rowH + rowGap);
      s.addShape("rect", {
        x: MARGIN, y, w: SW - 2 * MARGIN, h: rowH,
        fill: { color: i % 2 === 0 ? C.parchmentDeep : C.parchment },
        line: { color: C.parchmentDeep, width: 0 },
      });
      s.addShape("ellipse", {
        x: MARGIN + 0.2, y: y + 0.15, w: 0.6, h: 0.6,
        fill: { color: C.garnet }, line: { color: C.garnet, width: 0 },
      });
      s.addImage({ data: o.icon, x: MARGIN + 0.3, y: y + 0.25, w: 0.4, h: 0.4 });
      s.addText(o.title, {
        x: MARGIN + 1.0, y, w: 4.8, h: rowH,
        fontFace: FONT_HEAD, fontSize: 16, bold: true, color: C.garnet,
        align: "left", valign: "middle", margin: 0,
      });
      s.addText(o.body, {
        x: MARGIN + 5.9, y, w: SW - MARGIN - 5.9 - MARGIN - 0.2, h: rowH,
        fontFace: FONT_BODY, fontSize: 13, color: C.walnut,
        align: "left", valign: "middle", margin: 0,
      });
    });
  }

  // =============================================================
  // SLIDE 7 — Architecture: two systems, one platform (2-col + arrow)
  // =============================================================
  {
    const s = contentSlideBase(pres, 7, TOTAL, "Two systems, one platform");

    s.addText("Ops agents supply the changes. The CAB decides whether they ship.", {
      x: MARGIN, y: 1.75, w: SW - 2 * MARGIN, h: 0.5,
      fontFace: FONT_BODY, fontSize: 16, italic: true, color: C.walnut,
      align: "left", valign: "top", margin: 0,
    });

    const panelY = 2.6, panelH = 4.0, arrowW = 1.0;
    const panelW = (SW - 2 * MARGIN - arrowW) / 2;
    const x1 = MARGIN;
    const x2 = MARGIN + panelW + arrowW;

    // LEFT panel — Proposers
    s.addShape("rect", {
      x: x1, y: panelY, w: panelW, h: panelH,
      fill: { color: C.parchmentDeep }, line: { color: C.parchmentDeep, width: 0 },
    });
    s.addText("Ops Agents", {
      x: x1 + 0.4, y: panelY + 0.3, w: panelW - 0.8, h: 0.5,
      fontFace: FONT_HEAD, fontSize: 22, bold: true, color: C.garnet, margin: 0,
    });
    s.addText("the proposers", {
      x: x1 + 0.4, y: panelY + 0.8, w: panelW - 0.8, h: 0.35,
      fontFace: FONT_BODY, fontSize: 13, italic: true, color: C.mutedBrown, margin: 0,
    });
    const ops = [
      { icon: icnEye, title: "Monitoring Agent", body: "VM health, alerts, observability gaps" },
      { icon: icnSearchDollar, title: "Cost Agent", body: "Waste, orphaned disks, over-provisioning" },
      { icon: icnCogs, title: "Deploy Agent", body: "Security posture, NSG rules, compliance" },
    ];
    ops.forEach((o, i) => {
      const y = panelY + 1.35 + i * 0.85;
      s.addImage({ data: o.icon, x: x1 + 0.4, y: y + 0.05, w: 0.4, h: 0.4 });
      s.addText(o.title, {
        x: x1 + 1.0, y: y, w: panelW - 1.4, h: 0.35,
        fontFace: FONT_HEAD, fontSize: 15, bold: true, color: C.walnut, margin: 0,
      });
      s.addText(o.body, {
        x: x1 + 1.0, y: y + 0.35, w: panelW - 1.4, h: 0.35,
        fontFace: FONT_BODY, fontSize: 12, color: C.mutedBrown, margin: 0,
      });
    });

    // ARROW — brass
    s.addImage({
      data: icnArrow,
      x: MARGIN + panelW + 0.25, y: panelY + panelH / 2 - 0.25,
      w: 0.5, h: 0.5,
    });

    // RIGHT panel — Adjudicators
    s.addShape("rect", {
      x: x2, y: panelY, w: panelW, h: panelH,
      fill: { color: C.garnet }, line: { color: C.garnet, width: 0 },
    });
    s.addText("AI CAB", {
      x: x2 + 0.4, y: panelY + 0.3, w: panelW - 0.8, h: 0.5,
      fontFace: FONT_HEAD, fontSize: 22, bold: true, color: C.brass, margin: 0,
    });
    s.addText("the adjudicators", {
      x: x2 + 0.4, y: panelY + 0.8, w: panelW - 0.8, h: 0.35,
      fontFace: FONT_BODY, fontSize: 13, italic: true, color: "CBD5E1", margin: 0,
    });
    const cab = [
      { title: "Blast Radius", body: "Downstream impact" },
      { title: "Policy", body: "Governance compliance" },
      { title: "Historical", body: "Incident precedent" },
      { title: "Financial", body: "Cost volatility" },
    ];
    cab.forEach((c, i) => {
      const y = panelY + 1.35 + i * 0.6;
      // Brass dot
      s.addShape("ellipse", {
        x: x2 + 0.45, y: y + 0.18, w: 0.15, h: 0.15,
        fill: { color: C.brass }, line: { color: C.brass, width: 0 },
      });
      s.addText(c.title, {
        x: x2 + 0.8, y: y, w: 2.5, h: 0.5,
        fontFace: FONT_HEAD, fontSize: 15, bold: true, color: C.white, margin: 0,
        valign: "middle",
      });
      s.addText(c.body, {
        x: x2 + 3.1, y: y, w: panelW - 3.5, h: 0.5,
        fontFace: FONT_BODY, fontSize: 12, color: "CBD5E1", margin: 0,
        valign: "middle",
      });
    });
  }

  // =============================================================
  // SLIDE 8 — SRI™ framework (styled table)
  // =============================================================
  {
    const s = contentSlideBase(pres, 8, TOTAL, "The SRI™ framework");

    s.addText(
      "Four independent dimensions. One weighted composite. One defensible verdict.",
      {
        x: MARGIN, y: 1.75, w: SW - 2 * MARGIN, h: 0.5,
        fontFace: FONT_BODY, fontSize: 16, italic: true, color: C.walnut, margin: 0,
      }
    );

    const rows = [
      [
        { text: "Dimension", options: { bold: true, color: C.white, fill: { color: C.garnet }, fontFace: FONT_HEAD, fontSize: 14, align: "left", valign: "middle" } },
        { text: "What it measures", options: { bold: true, color: C.white, fill: { color: C.garnet }, fontFace: FONT_HEAD, fontSize: 14, align: "left", valign: "middle" } },
        { text: "Producing agent", options: { bold: true, color: C.white, fill: { color: C.garnet }, fontFace: FONT_HEAD, fontSize: 14, align: "left", valign: "middle" } },
      ],
      [
        { text: "SRI:Infrastructure", options: { bold: true, color: C.walnut, fill: { color: C.parchmentDeep }, fontFace: FONT_HEAD, fontSize: 14 } },
        { text: "Blast radius — downstream services, SPOFs, availability zones affected", options: { color: C.walnut, fill: { color: C.parchmentDeep }, fontFace: FONT_BODY, fontSize: 13 } },
        { text: "Blast Radius Agent", options: { color: C.mutedBrown, fill: { color: C.parchmentDeep }, fontFace: FONT_BODY, fontSize: 13, italic: true } },
      ],
      [
        { text: "SRI:Policy", options: { bold: true, color: C.walnut, fill: { color: C.parchment }, fontFace: FONT_HEAD, fontSize: 14 } },
        { text: "Governance compliance — policy match + severity", options: { color: C.walnut, fill: { color: C.parchment }, fontFace: FONT_BODY, fontSize: 13 } },
        { text: "Policy Agent", options: { color: C.mutedBrown, fill: { color: C.parchment }, fontFace: FONT_BODY, fontSize: 13, italic: true } },
      ],
      [
        { text: "SRI:Historical", options: { bold: true, color: C.walnut, fill: { color: C.parchmentDeep }, fontFace: FONT_HEAD, fontSize: 14 } },
        { text: "Precedent — BM25-ranked similarity to past incidents", options: { color: C.walnut, fill: { color: C.parchmentDeep }, fontFace: FONT_BODY, fontSize: 13 } },
        { text: "Historical Agent", options: { color: C.mutedBrown, fill: { color: C.parchmentDeep }, fontFace: FONT_BODY, fontSize: 13, italic: true } },
      ],
      [
        { text: "SRI:Cost", options: { bold: true, color: C.walnut, fill: { color: C.parchment }, fontFace: FONT_HEAD, fontSize: 14 } },
        { text: "Financial volatility — projected monthly cost delta", options: { color: C.walnut, fill: { color: C.parchment }, fontFace: FONT_BODY, fontSize: 13 } },
        { text: "Financial Agent", options: { color: C.mutedBrown, fill: { color: C.parchment }, fontFace: FONT_BODY, fontSize: 13, italic: true } },
      ],
    ];

    s.addTable(rows, {
      x: MARGIN, y: 2.5, w: SW - 2 * MARGIN,
      colW: [2.6, 6.5, 3.0],
      rowH: 0.7,
      border: { type: "none" },
    });

    s.addText("Every verdict is reconstructable from its inputs. Every score is auditable.", {
      x: MARGIN, y: 6.45, w: SW - 2 * MARGIN, h: 0.35,
      fontFace: FONT_HEAD, fontSize: 15, italic: true, color: C.garnet,
      align: "center", margin: 0,
    });
  }

  // =============================================================
  // SLIDE 9 — Decision surface (3 horizontal verdict bars)
  // =============================================================
  {
    const s = contentSlideBase(pres, 9, TOTAL, "The decision surface");

    s.addText(
      "Deterministic rules first. The LLM adjusts within a bounded ±30 range — with a guardrail that prevents model drift from ever dominating the verdict.",
      {
        x: MARGIN, y: 1.75, w: SW - 2 * MARGIN, h: 0.6,
        fontFace: FONT_BODY, fontSize: 15, color: C.walnut, margin: 0,
      }
    );

    const barY = 2.75, barH = 1.15, barGap = 0.15;
    const bars = [
      { icon: icnCheck, color: C.approved, label: "APPROVED", range: "SRI ≤ 25", body: "No HIGH / CRITICAL violation · Cleared for execution" },
      { icon: icnHourglass, color: C.escalated, label: "ESCALATED", range: "SRI 26 – 60", body: "…or any HIGH violation · Human review required" },
      { icon: icnX, color: C.denied, label: "DENIED", range: "SRI > 60", body: "…or any CRITICAL violation · Action blocked" },
    ];
    bars.forEach((b, i) => {
      const y = barY + i * (barH + barGap);
      // Coloured card
      s.addShape("rect", {
        x: MARGIN, y, w: SW - 2 * MARGIN, h: barH,
        fill: { color: C.parchmentDeep }, line: { color: C.parchmentDeep, width: 0 },
      });
      // Left colour accent
      s.addShape("rect", {
        x: MARGIN, y, w: 0.15, h: barH,
        fill: { color: b.color }, line: { color: b.color, width: 0 },
      });
      // Icon
      s.addImage({ data: b.icon, x: MARGIN + 0.45, y: y + 0.3, w: 0.55, h: 0.55 });
      // Label
      s.addText(b.label, {
        x: MARGIN + 1.2, y: y + 0.15, w: 2.4, h: 0.45,
        fontFace: FONT_HEAD, fontSize: 20, bold: true, color: b.color,
        align: "left", valign: "middle", margin: 0,
      });
      // Range
      s.addText(b.range, {
        x: MARGIN + 1.2, y: y + 0.6, w: 2.4, h: 0.4,
        fontFace: FONT_BODY, fontSize: 13, italic: true, color: C.mutedBrown,
        align: "left", valign: "top", margin: 0,
      });
      // Body
      s.addText(b.body, {
        x: MARGIN + 3.8, y, w: SW - MARGIN - 3.8 - MARGIN - 0.2, h: barH,
        fontFace: FONT_BODY, fontSize: 14, color: C.walnut,
        align: "left", valign: "middle", margin: 0,
      });
    });

    s.addText("Critical-severity policies always require human approval. The LLM cannot override them.", {
      x: MARGIN, y: 6.6, w: SW - 2 * MARGIN, h: 0.35,
      fontFace: FONT_HEAD, fontSize: 15, italic: true, color: C.garnet,
      align: "center", margin: 0,
    });
  }

  // =============================================================
  // SLIDE 10 — What's architecturally different (3-col cards)
  // =============================================================
  {
    const s = contentSlideBase(pres, 10, TOTAL, "Architecturally different");

    const cardY = 2.0, cardH = 4.5, gap = 0.3;
    const cardW = (SW - 2 * MARGIN - 2 * gap) / 3;
    const cards = [
      {
        icon: icnGavel,
        title: "LLM as decision-maker",
        body: "Most tools use the LLM to explain a rule-based verdict. RuriSkry lets the LLM adjust the verdict within a bounded range — with a hard clamp that prevents hallucination from dominating.",
      },
      {
        icon: icnLayers,
        title: "IaC-safe execution",
        body: "Approved changes do not execute directly on Azure — Terraform would revert them. They open a pull request against the IaC repo. Governance decides; Terraform executes; humans merge.",
      },
      {
        icon: icnFile,
        title: "Audit by default",
        body: "Every verdict, every score adjustment, every human approval — persisted to Cosmos DB. Counterfactual analysis on every decision: what would change this outcome?",
      },
    ];
    cards.forEach((c, i) => {
      const x = MARGIN + i * (cardW + gap);
      s.addShape("rect", {
        x, y: cardY, w: cardW, h: cardH,
        fill: { color: C.parchmentDeep }, line: { color: C.parchmentDeep, width: 0 },
      });
      // Top brass band
      s.addShape("rect", {
        x, y: cardY, w: cardW, h: 0.1,
        fill: { color: C.brass }, line: { color: C.brass, width: 0 },
      });
      // Icon
      s.addImage({ data: c.icon, x: x + 0.4, y: cardY + 0.45, w: 0.7, h: 0.7 });
      // Title
      s.addText(c.title, {
        x: x + 0.4, y: cardY + 1.3, w: cardW - 0.8, h: 0.9,
        fontFace: FONT_HEAD, fontSize: 20, bold: true, color: C.garnet,
        align: "left", valign: "top", margin: 0,
      });
      // Body
      s.addText(c.body, {
        x: x + 0.4, y: cardY + 2.25, w: cardW - 0.8, h: cardH - 2.4,
        fontFace: FONT_BODY, fontSize: 14, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      });
    });
  }

  // =============================================================
  // SLIDE 11 — Where it fits in an enterprise stack (horizontal flow)
  // =============================================================
  {
    const s = contentSlideBase(pres, 11, TOTAL, "Where it fits in an enterprise stack");

    // Horizontal pipeline: Ops Agent → RuriSkry → IaC/Azure → Change
    const flowY = 2.2, boxH = 1.2;
    const boxes = [
      { label: "Ops Agent", color: C.parchmentDeep, textColor: C.walnut },
      { label: "RuriSkry\ngovernance", color: C.garnet, textColor: C.white, bold: true },
      { label: "IaC / Azure", color: C.parchmentDeep, textColor: C.walnut },
      { label: "Change applied", color: C.parchmentDeep, textColor: C.walnut },
    ];
    const boxW = 2.4, arrowW = 0.55;
    const totalFlowW = boxes.length * boxW + (boxes.length - 1) * arrowW;
    const startFlowX = (SW - totalFlowW) / 2;

    boxes.forEach((b, i) => {
      const x = startFlowX + i * (boxW + arrowW);
      s.addShape("rect", {
        x, y: flowY, w: boxW, h: boxH,
        fill: { color: b.color }, line: { color: b.color, width: 0 },
      });
      s.addText(b.label, {
        x, y: flowY, w: boxW, h: boxH,
        fontFace: FONT_HEAD, fontSize: b.bold ? 18 : 16, bold: !!b.bold,
        color: b.textColor, align: "center", valign: "middle", margin: 0,
      });
      if (i < boxes.length - 1) {
        s.addImage({
          data: icnArrow,
          x: x + boxW + (arrowW - 0.35) / 2, y: flowY + boxH / 2 - 0.175,
          w: 0.35, h: 0.35,
        });
      }
    });

    // 2 column lists below: Complements / Does NOT replace
    const listY = 4.2, listH = 2.4, listGap = 0.4;
    const listW = (SW - 2 * MARGIN - listGap) / 2;

    // Complements
    s.addText("Complements, does not replace", {
      x: MARGIN, y: listY, w: listW, h: 0.4,
      fontFace: FONT_HEAD, fontSize: 16, bold: true, color: C.garnet, margin: 0,
    });
    s.addText([
      { text: "Azure Policy, Defender, Advisor ", options: { bold: true, breakLine: false } },
      { text: "— signal sources, not decision layers", options: { breakLine: true } },
      { text: "CI / CD pipelines ", options: { bold: true, breakLine: false } },
      { text: "— RuriSkry triggers the PR, CI / CD runs it", options: { breakLine: true } },
      { text: "Existing human CAB ", options: { bold: true, breakLine: false } },
      { text: "— handles the subset humans don't have bandwidth for", options: {} },
    ], {
      x: MARGIN, y: listY + 0.5, w: listW, h: listH - 0.5,
      fontFace: FONT_BODY, fontSize: 13, color: C.walnut,
      align: "left", valign: "top", paraSpaceAfter: 6, margin: 0,
    });

    // Does NOT replace
    const x2 = MARGIN + listW + listGap;
    s.addText("Does not replace", {
      x: x2, y: listY, w: listW, h: 0.4,
      fontFace: FONT_HEAD, fontSize: 16, bold: true, color: C.garnet, margin: 0,
    });
    s.addText([
      { text: "Identity and access management (IAM)", options: { breakLine: true } },
      { text: " ", options: { fontSize: 4, breakLine: true } },
      { text: "Your IaC tool of choice (Terraform, Bicep)", options: { breakLine: true } },
      { text: " ", options: { fontSize: 4, breakLine: true } },
      { text: "Your existing change management process", options: {} },
    ], {
      x: x2, y: listY + 0.5, w: listW, h: listH - 0.5,
      fontFace: FONT_BODY, fontSize: 13, color: C.walnut,
      align: "left", valign: "top", paraSpaceAfter: 6, margin: 0,
    });
  }

  // =============================================================
  // SLIDE 12 — Status today (badge grid 2x3)
  // =============================================================
  {
    const s = contentSlideBase(pres, 12, TOTAL, "Status today");

    const items = [
      { icon: icnGithub, title: "Open source", body: "MIT licensed · public on GitHub" },
      { icon: icnCloud, title: "Azure-native", body: "Container Apps · Cosmos DB · AI Search · Key Vault · Resource Graph · Monitor" },
      { icon: icnShield, title: "Production-architected", body: "1,000+ test suite · deployed end-to-end on Azure · small-scale validated, ready for broader field testing" },
      { icon: icnBolt, title: "Fully async", body: "7 agents · parallel orchestration · sub-10-second verdicts" },
      { icon: icnEye, title: "Dashboard included", body: "6-page React UI · SSE-streamed · enterprise design system" },
      { icon: icnCogs, title: "One-command deploy", body: "Terraform + Container Apps + Static Web App in a single script" },
    ];

    const cols = 3, rows = 2;
    const gridY = 1.9, gridH = 4.2, gap = 0.25;
    const cellW = (SW - 2 * MARGIN - (cols - 1) * gap) / cols;
    const cellH = (gridH - (rows - 1) * gap) / rows;

    items.forEach((item, i) => {
      const col = i % cols, row = Math.floor(i / cols);
      const x = MARGIN + col * (cellW + gap);
      const y = gridY + row * (cellH + gap);
      s.addShape("rect", {
        x, y, w: cellW, h: cellH,
        fill: { color: C.parchmentDeep }, line: { color: C.parchmentDeep, width: 0 },
      });
      // Left brass accent
      s.addShape("rect", {
        x, y, w: 0.08, h: cellH,
        fill: { color: C.brass }, line: { color: C.brass, width: 0 },
      });
      s.addImage({ data: item.icon, x: x + 0.3, y: y + 0.3, w: 0.55, h: 0.55 });
      s.addText(item.title, {
        x: x + 1.05, y: y + 0.25, w: cellW - 1.3, h: 0.5,
        fontFace: FONT_HEAD, fontSize: 17, bold: true, color: C.garnet,
        align: "left", valign: "top", margin: 0,
      });
      s.addText(item.body, {
        x: x + 1.05, y: y + 0.75, w: cellW - 1.3, h: cellH - 0.9,
        fontFace: FONT_BODY, fontSize: 12, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      });
    });

    s.addText(
      "Originally built for the Microsoft AI Dev Days Hackathon 2026 (Feb – Mar 2026). Validated at small scale; the architecture is ready for broader real-world testing.",
      {
        x: MARGIN, y: 6.4, w: SW - 2 * MARGIN, h: 0.5,
        fontFace: FONT_BODY, fontSize: 13, italic: true, color: C.mutedBrown,
        align: "center", margin: 0,
      }
    );
  }

  // =============================================================
  // SLIDE 13 — Roadmap (2-col)
  // =============================================================
  {
    const s = contentSlideBase(pres, 13, TOTAL, "Roadmap — what is next");

    s.addText(
      "Two architectural gaps surfaced from early external review. Both land in the next phase.",
      {
        x: MARGIN, y: 1.75, w: SW - 2 * MARGIN, h: 0.5,
        fontFace: FONT_BODY, fontSize: 15, italic: true, color: C.walnut, margin: 0,
      }
    );

    const colY = 2.5, colH = 4.0, gap = 0.4;
    const colW = (SW - 2 * MARGIN - gap) / 2;

    // Col 1 — Evidence-aware scoring
    const x1 = MARGIN;
    s.addShape("rect", {
      x: x1, y: colY, w: colW, h: colH,
      fill: { color: C.parchmentDeep }, line: { color: C.parchmentDeep, width: 0 },
    });
    s.addShape("rect", {
      x: x1, y: colY, w: colW, h: 0.1,
      fill: { color: C.brass }, line: { color: C.brass, width: 0 },
    });
    s.addImage({ data: icnSearchDollar, x: x1 + 0.4, y: colY + 0.35, w: 0.65, h: 0.65 });
    s.addText("Evidence-aware scoring", {
      x: x1 + 0.4, y: colY + 1.15, w: colW - 0.8, h: 0.5,
      fontFace: FONT_HEAD, fontSize: 20, bold: true, color: C.garnet, margin: 0,
    });
    s.addText(
      "Proposer-side confidence becomes a first-class scoring input. Low-confidence agent proposals widen the escalation band — even when composite risk is low. Raises the bar for auto-approval without lowering throughput for confident calls.",
      {
        x: x1 + 0.4, y: colY + 1.75, w: colW - 0.8, h: colH - 1.95,
        fontFace: FONT_BODY, fontSize: 14, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      }
    );

    // Col 2 — Conditional approvals
    const x2 = MARGIN + colW + gap;
    s.addShape("rect", {
      x: x2, y: colY, w: colW, h: colH,
      fill: { color: C.parchmentDeep }, line: { color: C.parchmentDeep, width: 0 },
    });
    s.addShape("rect", {
      x: x2, y: colY, w: colW, h: 0.1,
      fill: { color: C.brass }, line: { color: C.brass, width: 0 },
    });
    s.addImage({ data: icnClipboard, x: x2 + 0.4, y: colY + 0.35, w: 0.65, h: 0.65 });
    s.addText("Conditional approvals", {
      x: x2 + 0.4, y: colY + 1.15, w: colW - 0.8, h: 0.5,
      fontFace: FONT_HEAD, fontSize: 20, bold: true, color: C.garnet, margin: 0,
    });
    s.addText(
      [
        { text: "Verdicts move beyond Approve / Escalate / Deny toward structured conditions:", options: { breakLine: true } },
        { text: " ", options: { fontSize: 4, breakLine: true } },
        { text: "Maintenance window required", options: { bullet: true, breakLine: true } },
        { text: "Canary rollout required", options: { bullet: true, breakLine: true } },
        { text: "Rollback pre-staged", options: { bullet: true, breakLine: true } },
        { text: "Named approver required", options: { bullet: true, breakLine: true } },
        { text: " ", options: { fontSize: 4, breakLine: true } },
        { text: "The Execution Gateway enforces each condition before anything reaches Azure.", options: { italic: true } },
      ],
      {
        x: x2 + 0.4, y: colY + 1.75, w: colW - 0.8, h: colH - 1.95,
        fontFace: FONT_BODY, fontSize: 13, color: C.walnut,
        align: "left", valign: "top", margin: 0, paraSpaceAfter: 4,
      }
    );
  }

  // =============================================================
  // SLIDE 14 — Where this is heading (3 numbered claims + closing)
  // =============================================================
  {
    const s = contentSlideBase(pres, 14, TOTAL, "Where this is heading");

    s.addText(
      "AI agents are being deployed into production infrastructure faster than governance can catch up. Three beats to watch:",
      {
        x: MARGIN, y: 1.75, w: SW - 2 * MARGIN, h: 0.55,
        fontFace: FONT_BODY, fontSize: 15, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      }
    );

    const claims = [
      {
        n: "1",
        title: "Adoption is accelerating.",
        body: "Every major cloud now ships operational AI agents. Enterprises are wiring them into production, whether or not governance is ready.",
      },
      {
        n: "2",
        title: "Capability controls are commoditising.",
        body: "IAM, permission scopes, rate limits — every vendor ships them. The judgment layer (blast radius, policy fit, precedent, cost) does not exist.",
      },
      {
        n: "3",
        title: "Procurement will follow.",
        body: "Same pattern as IAM, endpoint detection, and cloud security posture management: a missing control layer becomes required procurement within 12–18 months.",
      },
    ];

    const rowY = 2.55, rowH = 1.15, rowGap = 0.12;
    claims.forEach((c, i) => {
      const y = rowY + i * (rowH + rowGap);
      // Row surface
      s.addShape("rect", {
        x: MARGIN, y, w: SW - 2 * MARGIN, h: rowH,
        fill: { color: C.parchmentDeep }, line: { color: C.parchmentDeep, width: 0 },
      });
      // Left accent band
      s.addShape("rect", {
        x: MARGIN, y, w: 0.1, h: rowH,
        fill: { color: C.brass }, line: { color: C.brass, width: 0 },
      });
      // Numeral
      s.addText(c.n, {
        x: MARGIN + 0.35, y, w: 1.0, h: rowH,
        fontFace: FONT_HEAD, fontSize: 52, bold: true, color: C.garnet,
        align: "left", valign: "middle", margin: 0,
      });
      // Title
      s.addText(c.title, {
        x: MARGIN + 1.45, y: y + 0.18, w: SW - MARGIN - 1.45 - MARGIN - 0.2, h: 0.45,
        fontFace: FONT_HEAD, fontSize: 19, bold: true, color: C.garnet,
        align: "left", valign: "middle", margin: 0,
      });
      // Body
      s.addText(c.body, {
        x: MARGIN + 1.45, y: y + 0.6, w: SW - MARGIN - 1.45 - MARGIN - 0.2, h: rowH - 0.7,
        fontFace: FONT_BODY, fontSize: 13, color: C.walnut,
        align: "left", valign: "top", margin: 0,
      });
    });

    // Closing line
    s.addText(
      "RuriSkry is our working answer for Azure — open source, so the pattern can spread while the category gets built out.",
      {
        x: MARGIN, y: 6.4, w: SW - 2 * MARGIN, h: 0.45,
        fontFace: FONT_HEAD, fontSize: 15, italic: true, color: C.garnet,
        align: "center", valign: "middle", margin: 0,
      }
    );
  }

  // =============================================================
  // SLIDE 15 — Thank you / call to action (garnet closing)
  // =============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.garnet };

    s.addText("Thank you", {
      x: 0, y: 1.3, w: SW, h: 1.0,
      fontFace: FONT_HEAD, fontSize: 48, italic: true, color: C.white, align: "center",
    });

    s.addText("Questions I would like to explore", {
      x: 0, y: 2.6, w: SW, h: 0.5,
      fontFace: FONT_HEAD, fontSize: 20, color: C.brass, align: "center",
    });

    const qs = [
      "Where does this fit against what we are doing with AI agents internally?",
      "Is the CAB pattern something we should productise further?",
      "Is there a fit with customers starting to deploy AI agents?",
    ];
    qs.forEach((q, i) => {
      s.addText(`“${q}”`, {
        x: 1.5, y: 3.4 + i * 0.65, w: SW - 3.0, h: 0.6,
        fontFace: FONT_HEAD, fontSize: 17, italic: true, color: C.white,
        align: "center", valign: "middle", margin: 0,
      });
    });

    // Brass divider
    s.addShape("rect", {
      x: SW / 2 - 0.5, y: 5.65, w: 1.0, h: 0.04,
      fill: { color: C.brass }, line: { color: C.brass, width: 0 },
    });

    s.addText("github.com/psc0des/ruriskry", {
      x: 0, y: 6.0, w: SW, h: 0.5,
      fontFace: FONT_BODY, fontSize: 16, bold: true, color: C.brass, align: "center",
    });
  }

  // -----------------------------------------------------------
  // Write file
  // -----------------------------------------------------------
  const outPath = path.resolve(__dirname, "../../docs/RuriSkry-Exec-Deck.pptx");
  await pres.writeFile({ fileName: outPath });
  console.log(`\n✓ Wrote ${outPath}`);
  console.log(`  Size: ${(fs.statSync(outPath).size / 1024).toFixed(1)} KB`);
})().catch((err) => {
  console.error("Build failed:", err);
  process.exit(1);
});
