function normalizeString(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function normalizeStringArray(values) {
  if (!Array.isArray(values)) return [];
  const seen = new Set();
  const output = [];
  for (const value of values) {
    const normalized = normalizeString(value);
    if (!normalized) continue;
    const key = normalized.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    output.push(normalized);
  }
  return output;
}

function normalizeObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function boundedInteger(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, Math.floor(parsed)));
}

function startCase(value) {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function toHashtag(value) {
  const normalized = normalizeString(value);
  if (!normalized) return null;
  const compact = normalized.replace(/[^a-zA-Z0-9 ]+/g, ' ').split(/\s+/).filter(Boolean).map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`).join('');
  return compact ? `#${compact}` : null;
}

function joinList(values, fallback = 'stronger systems') {
  const list = normalizeStringArray(values);
  if (list.length === 0) return fallback;
  if (list.length === 1) return list[0];
  if (list.length === 2) return `${list[0]} and ${list[1]}`;
  return `${list.slice(0, -1).join(', ')}, and ${list[list.length - 1]}`;
}

function isoAtUtc(startDate, dayOffset = 0, scheduledHourUtc = 16, scheduledMinuteUtc = 0, cadenceDays = 1) {
  const base = normalizeString(startDate) ? new Date(startDate) : new Date();
  const safeBase = Number.isNaN(base.getTime()) ? new Date() : base;
  const scheduled = new Date(Date.UTC(
    safeBase.getUTCFullYear(),
    safeBase.getUTCMonth(),
    safeBase.getUTCDate() + (dayOffset * cadenceDays),
    scheduledHourUtc,
    scheduledMinuteUtc,
    0,
    0
  ));
  return scheduled.toISOString();
}

const PILLARS = [
  {
    id: 'clarity',
    label: 'market clarity',
    preferredStyles: ['infographic', 'editorial'],
    imagePrompt: 'clean premium dashboard and notes arranged on a modern desk, polished brand lighting',
    hooks: [
      'Most growth problems do not start with effort. They start with unclear priorities.',
      'When the next step is fuzzy, even good teams lose momentum.',
      'A lot of businesses do not need more activity. They need more clarity.'
    ],
    bodies: [
      ({ businessName, audience, offerPhrase }) => `${businessName} helps ${audience} simplify the path around ${offerPhrase} so the right decisions get easier to make.`,
      ({ businessName, goalPhrase, offerPhrase }) => `${businessName} focuses on ${offerPhrase} so teams can move with more confidence toward ${goalPhrase}.`,
      ({ audience, differentiatorPhrase }) => `For ${audience}, momentum usually improves when ${differentiatorPhrase} becomes part of the day-to-day system.`
    ],
    closers: [
      'Clarity compounds faster than noise.',
      'Simple usually scales better than scattered.',
      'Better decisions usually follow better visibility.'
    ],
    tags: ['BusinessGrowth', 'Strategy', 'Clarity']
  },
  {
    id: 'pain-point',
    label: 'pain point',
    preferredStyles: ['realistic-scene', 'editorial'],
    imagePrompt: 'business owner dealing with scattered tasks and notifications in a refined modern workspace',
    hooks: [
      'The real bottleneck is often not demand. It is friction.',
      'A messy handoff can undo a lot of good marketing.',
      'Plenty of revenue leaks happen after someone already showed interest.'
    ],
    bodies: [
      ({ offerPhrase, audience }) => `If ${audience} are dealing with slow follow-up, unclear ownership, or uneven ${offerPhrase}, the fix is usually operational before it is promotional.`,
      ({ businessName, differentiatorPhrase }) => `${businessName} looks for the places where ${differentiatorPhrase} can remove drag before it turns into lost trust.`,
      ({ goalPhrase }) => `Businesses get closer to ${goalPhrase} when the experience between first touch and final outcome feels easier to move through.`
    ],
    closers: [
      'Less friction usually means better conversion and better retention.',
      'The smoother the path feels, the easier trust becomes.',
      'A cleaner process can be a growth lever all by itself.'
    ],
    tags: ['Operations', 'CustomerExperience', 'BusinessSystems']
  },
  {
    id: 'process',
    label: 'process insight',
    preferredStyles: ['hybrid-system', 'infographic'],
    imagePrompt: 'sleek process map with premium interface cards and calm studio lighting',
    hooks: [
      'Good results usually come from repeatable steps, not heroic effort.',
      'The best systems reduce guesswork for both the team and the customer.',
      'A strong process should make the next action obvious.'
    ],
    bodies: [
      ({ offerPhrase, audience }) => `That is why ${offerPhrase} should be built to support ${audience} with fewer delays, clearer ownership, and steadier execution.`,
      ({ businessName, differentiatorPhrase }) => `${businessName} prefers workflows where ${differentiatorPhrase} shows up inside the process instead of living in disconnected tools.`,
      ({ goalPhrase }) => `Repeatable systems are one of the fastest ways to get closer to ${goalPhrase} without adding more chaos.`
    ],
    closers: [
      'Smooth beats complicated.',
      'Repeatable is easier to improve.',
      'If the system is clearer, the outcome usually is too.'
    ],
    tags: ['Process', 'Operations', 'Efficiency']
  },
  {
    id: 'myth',
    label: 'myth busting',
    preferredStyles: ['editorial', 'infographic'],
    imagePrompt: 'bold editorial layout with premium contrast, crisp typography, and confident business mood',
    hooks: [
      'A common myth is that growth always needs more tools.',
      'More complexity is not the same as more sophistication.',
      'Not every problem needs a bigger stack. Some need a better flow.'
    ],
    bodies: [
      ({ audience, offerPhrase }) => `For ${audience}, better outcomes usually come from aligning ${offerPhrase}, responsibilities, and follow-through before adding something new.`,
      ({ businessName, goalPhrase }) => `${businessName} cares more about usable systems than shiny ones, because that is what moves teams toward ${goalPhrase}.`,
      ({ differentiatorPhrase }) => `The real edge is often simple execution, especially when ${differentiatorPhrase} becomes consistent.`
    ],
    closers: [
      'Simple systems are easier to trust.',
      'Useful beats impressive every time.',
      'The best setup is the one people actually use.'
    ],
    tags: ['Strategy', 'Operations', 'BusinessGrowth']
  },
  {
    id: 'offer',
    label: 'offer spotlight',
    preferredStyles: ['realistic-scene', 'hybrid-system'],
    imagePrompt: 'premium brand presentation of service offer with clean productized business visuals',
    hooks: [
      'A strong offer should make the value feel concrete.',
      'The easier an offer is to understand, the easier it is to trust.',
      'People respond faster when the next step feels specific.'
    ],
    bodies: [
      ({ businessName, primaryOffer, audience }) => `${businessName} is built around ${primaryOffer} for ${audience} who want better execution without extra confusion.`,
      ({ offerPhrase, goalPhrase }) => `${offerPhrase} works best when it is tied directly to ${goalPhrase}, not just to activity for its own sake.`,
      ({ differentiatorPhrase }) => `Clear positioning matters, especially when ${differentiatorPhrase} is part of what makes the service feel easier to say yes to.`
    ],
    closers: [
      'Specific offers create better conversations.',
      'Clear value tends to travel further.',
      'A tighter message makes better buying easier.'
    ],
    tags: ['OfferStrategy', 'Positioning', 'BrandClarity']
  },
  {
    id: 'trust',
    label: 'trust building',
    preferredStyles: ['realistic-scene', 'editorial'],
    imagePrompt: 'confident team collaboration in a premium office environment with calm natural light',
    hooks: [
      'Trust usually grows when the experience feels reliable.',
      'Confidence is built in the small moments, not just the big promises.',
      'Consistency is one of the most underrated trust signals in business.'
    ],
    bodies: [
      ({ audience, offerPhrase }) => `For ${audience}, that means cleaner communication, better follow-through, and more dependable ${offerPhrase}.`,
      ({ businessName, differentiatorPhrase }) => `${businessName} aims for systems where ${differentiatorPhrase} is visible in the customer experience, not just in the sales message.`,
      ({ goalPhrase }) => `Reliable execution tends to move businesses closer to ${goalPhrase} because it gives people a reason to stay confident.`
    ],
    closers: [
      'Trust likes proof more than promises.',
      'Reliable beats flashy when decisions matter.',
      'Strong relationships are usually built on steadiness.'
    ],
    tags: ['Trust', 'CustomerExperience', 'Brand']
  },
  {
    id: 'faq',
    label: 'faq',
    preferredStyles: ['editorial', 'hybrid-system'],
    imagePrompt: 'refined question and answer layout with premium business magazine styling',
    hooks: [
      'One question worth asking is this: what actually needs to change first?',
      'A better question than what tool should we buy is what problem are we fixing?',
      'When businesses feel stuck, the right first question matters a lot.'
    ],
    bodies: [
      ({ offerPhrase, audience }) => `Usually the first step is finding the handoff, bottleneck, or message gap that is making ${offerPhrase} harder for ${audience} than it needs to be.`,
      ({ businessName, goalPhrase }) => `${businessName} starts with the clearest bottleneck because fast wins tend to unlock progress toward ${goalPhrase}.`,
      ({ differentiatorPhrase }) => `It is easier to improve the right thing when ${differentiatorPhrase} is visible enough to diagnose.`
    ],
    closers: [
      'Start where the friction is most expensive.',
      'The best first fix is usually the one you can feel every day.',
      'Good diagnosis saves a lot of wasted effort.'
    ],
    tags: ['FAQ', 'BusinessStrategy', 'Systems']
  },
  {
    id: 'quick-win',
    label: 'quick win',
    preferredStyles: ['infographic', 'editorial'],
    imagePrompt: 'minimal premium checklist and performance notes with bright accent lighting',
    hooks: [
      'One practical win is to remove one unnecessary step from the customer path this week.',
      'A fast improvement is often hiding inside a slow handoff.',
      'Small operational upgrades can create surprisingly visible momentum.'
    ],
    bodies: [
      ({ audience, primaryOffer }) => `For ${audience}, simplifying how ${primaryOffer} gets explained, booked, or fulfilled can pay off quickly.`,
      ({ businessName, differentiatorPhrase }) => `${businessName} looks for small system upgrades where ${differentiatorPhrase} can show up sooner.`,
      ({ goalPhrase }) => `Quick wins matter because they make the path toward ${goalPhrase} feel real instead of theoretical.`
    ],
    closers: [
      'Faster wins build better momentum.',
      'Simple improvements are still improvements.',
      'You do not need a full rebuild to create progress.'
    ],
    tags: ['QuickWins', 'Operations', 'Growth']
  },
  {
    id: 'results',
    label: 'results framing',
    preferredStyles: ['hybrid-system', 'realistic-scene'],
    imagePrompt: 'premium business performance scene with refined charts and steady upward movement',
    hooks: [
      'Good results feel clearer before they feel bigger.',
      'The strongest growth usually looks coordinated, not chaotic.',
      'Progress is easier to sustain when the signals are easy to read.'
    ],
    bodies: [
      ({ offerPhrase, goalPhrase }) => `When ${offerPhrase} is aligned properly, businesses get a cleaner shot at ${goalPhrase} with fewer wasted motions.`,
      ({ businessName, audience }) => `${businessName} helps ${audience} build the kind of operating rhythm that makes improvement easier to spot and repeat.`,
      ({ differentiatorPhrase }) => `Results get easier to defend when ${differentiatorPhrase} is supported by the actual system behind the scenes.`
    ],
    closers: [
      'Better systems make better outcomes easier to repeat.',
      'Visibility is part of performance.',
      'Steady results usually start with steadier operations.'
    ],
    tags: ['Results', 'BusinessGrowth', 'Performance']
  },
  {
    id: 'behind-scenes',
    label: 'behind the scenes',
    preferredStyles: ['realistic-scene', 'hybrid-system'],
    imagePrompt: 'behind the scenes view of premium team workflow, notes, devices, and calm operational setup',
    hooks: [
      'A lot of the work people never see is what makes the visible results possible.',
      'Behind most smooth customer experiences is a quiet system doing its job well.',
      'The invisible part of the business often determines how the visible part feels.'
    ],
    bodies: [
      ({ businessName, offerPhrase }) => `${businessName} thinks a lot about the operating layer behind ${offerPhrase}, because that is where consistency and speed start to show up.`,
      ({ audience, differentiatorPhrase }) => `For ${audience}, the behind-the-scenes advantage is usually that ${differentiatorPhrase} keeps execution from drifting.`,
      ({ goalPhrase }) => `Internal clarity matters because it supports the customer-facing experience that eventually drives ${goalPhrase}.`
    ],
    closers: [
      'The back end shapes the front-end experience.',
      'Better operations make better delivery possible.',
      'A strong engine makes the brand easier to believe.'
    ],
    tags: ['BehindTheScenes', 'Operations', 'Execution']
  },
  {
    id: 'objection',
    label: 'objection handling',
    preferredStyles: ['editorial', 'realistic-scene'],
    imagePrompt: 'confident premium business message with clear contrast, calm reassurance, and editorial composition',
    hooks: [
      'If now does not feel like the right time to improve the system, that is usually the signal that it matters.',
      'Waiting for less chaos before fixing the process can keep the chaos in place.',
      'A common hesitation is thinking the team has to stop everything before improving anything.'
    ],
    bodies: [
      ({ offerPhrase, audience }) => `In reality, ${offerPhrase} should make work easier for ${audience} in stages, not require a dramatic reset.`,
      ({ businessName, differentiatorPhrase }) => `${businessName} aims for progress that fits real operations, especially when ${differentiatorPhrase} is part of the goal.`,
      ({ goalPhrase }) => `Incremental changes can still create real movement toward ${goalPhrase}.`
    ],
    closers: [
      'Progress does not need to be dramatic to be valuable.',
      'Small upgrades still count.',
      'The best system changes usually feel practical, not theatrical.'
    ],
    tags: ['DecisionMaking', 'Operations', 'BusinessGrowth']
  },
  {
    id: 'cta',
    label: 'call to action',
    preferredStyles: ['editorial', 'realistic-scene'],
    imagePrompt: 'premium invitation scene with refined brand visuals and clean focal call to action',
    hooks: [
      'If one part of the business feels harder than it should, that is usually where the next conversation starts.',
      'When the system feels heavier than the outcome, it is worth looking closer.',
      'Sometimes the best next move is simply getting the bottleneck into the open.'
    ],
    bodies: [
      ({ businessName, audience, primaryOffer }) => `${businessName} works with ${audience} that want a clearer path around ${primaryOffer} without adding more confusion to the week.`,
      ({ offerPhrase, goalPhrase }) => `A focused look at ${offerPhrase} can reveal faster ways to move toward ${goalPhrase}.`,
      ({ differentiatorPhrase }) => `That conversation tends to be most useful when ${differentiatorPhrase} matters to the business long term.`
    ],
    closers: [
      'A clear next step beats a vague intention.',
      'The right conversation can remove a lot of drag.',
      'Start with the bottleneck you feel most often.'
    ],
    tags: ['CallToAction', 'Growth', 'Strategy']
  }
];

function normalizeBusinessProfile(input = {}) {
  const business = normalizeObject(input);
  const name = normalizeString(business.name) || normalizeString(business.businessName) || 'The business';
  const industry = normalizeString(business.industry) || normalizeString(business.category) || 'service business';
  const audience = normalizeStringArray(business.audience || business.audiences);
  const offers = normalizeStringArray(business.offers || business.services || business.solutions);
  const differentiators = normalizeStringArray(business.differentiators || business.strengths || business.advantages);
  const goals = normalizeStringArray(business.goals || business.outcomes);
  const ctas = normalizeStringArray(business.ctas || business.callsToAction);
  const hashtags = normalizeStringArray(business.hashtags);
  const knowledgeBaseRef = normalizeString(business.knowledgeBaseRef) || normalizeString(business.knowledgeBaseId) || null;

  return {
    name,
    industry,
    audience: audience.length > 0 ? audience : ['businesses that want a clearer path to growth'],
    offers: offers.length > 0 ? offers : ['clearer positioning, smoother operations, and better follow-up'],
    differentiators: differentiators.length > 0 ? differentiators : ['clarity, consistency, and practical execution'],
    goals: goals.length > 0 ? goals : ['more qualified conversations and steadier growth'],
    ctas,
    hashtags,
    knowledgeBaseRef
  };
}

function normalizeCalendarRequest(input = {}) {
  const request = normalizeObject(input);
  const calendar = normalizeObject(request.calendar);
  return {
    locationId: normalizeString(request.locationId) || null,
    accountIds: normalizeStringArray(request.accountIds || calendar.accountIds),
    status: normalizeString(request.status) || normalizeString(calendar.status) || 'draft',
    postDefaults: normalizeObject(request.postDefaults || calendar.postDefaults),
    creativeStyles: normalizeStringArray(request.creativeStyles || calendar.creativeStyles),
    postCount: positiveInteger(calendar.postCount || request.postCount, 30),
    cadenceDays: positiveInteger(calendar.cadenceDays || request.cadenceDays, 1),
    startDate: normalizeString(calendar.startDate) || normalizeString(request.startDate) || new Date().toISOString(),
    timezone: normalizeString(calendar.timezone) || normalizeString(request.timezone) || 'UTC',
    scheduledHourUtc: boundedInteger(calendar.scheduledHourUtc || request.scheduledHourUtc, 16, 0, 23),
    scheduledMinuteUtc: boundedInteger(calendar.scheduledMinuteUtc || request.scheduledMinuteUtc, 0, 0, 59)
  };
}

function buildContext(business) {
  return {
    businessName: business.name,
    industryPhrase: business.industry,
    audience: business.audience[0],
    audiencePhrase: joinList(business.audience, 'businesses that want a clearer path to growth'),
    primaryOffer: business.offers[0],
    offerPhrase: joinList(business.offers, 'clearer positioning and steadier follow-up'),
    differentiatorPhrase: joinList(business.differentiators, 'clarity, consistency, and practical execution'),
    goalPhrase: joinList(business.goals, 'more qualified conversations and steadier growth')
  };
}

function deriveHashtags(business, pillar) {
  const industryTag = toHashtag(business.industry);
  const defaults = pillar.tags.map((tag) => `#${tag}`);
  const explicit = business.hashtags.map((tag) => tag.startsWith('#') ? tag : `#${tag.replace(/^#+/, '')}`);
  return normalizeStringArray([...explicit, industryTag, ...defaults]).slice(0, 4);
}

function selectStyles(calendar, pillar, index) {
  const explicitVariants = calendar.creativeStyles.length > 0 ? calendar.creativeStyles : pillar.preferredStyles;
  const uniqueVariants = normalizeStringArray([...pillar.preferredStyles, ...explicitVariants]);
  const rotation = uniqueVariants.length > 0 ? uniqueVariants : ['editorial'];
  return {
    creativeStyle: rotation[index % rotation.length],
    creativeStyles: rotation.slice(0, Math.min(3, rotation.length))
  };
}

function buildSummary(pillar, business, context, index) {
  const hook = pillar.hooks[index % pillar.hooks.length];
  const body = pillar.bodies[index % pillar.bodies.length](context);
  const closer = pillar.closers[index % pillar.closers.length];
  const cta = business.ctas.length > 0 ? business.ctas[index % business.ctas.length] : 'If you want a clearer next step, start by identifying the bottleneck that shows up most often.';
  const hashtags = deriveHashtags(business, pillar).join(' ');
  return `${hook}\n\n${body}\n\n${closer}\n\n${cta}\n\n${hashtags}`;
}

export function generateSocialCalendarPlan(input = {}) {
  const business = normalizeBusinessProfile(input.business || input.businessProfile || {});
  const calendar = normalizeCalendarRequest(input);
  const context = buildContext(business);
  const posts = [];

  for (let index = 0; index < calendar.postCount; index += 1) {
    const pillar = PILLARS[index % PILLARS.length];
    const styles = selectStyles(calendar, pillar, index);
    const scheduleDate = isoAtUtc(calendar.startDate, index, calendar.scheduledHourUtc, calendar.scheduledMinuteUtc, calendar.cadenceDays);
    const summary = buildSummary(pillar, business, context, index);

    posts.push({
      day: index + 1,
      scheduleDate,
      pillarId: pillar.id,
      pillar: pillar.label,
      businessName: business.name,
      creativeStyle: styles.creativeStyle,
      creativeStyles: styles.creativeStyles,
      summary,
      imagePrompt: `${pillar.imagePrompt}, tailored for ${business.industryPhrase || business.industry}`
    });
  }

  const mutationRequest = {
    action: 'create_social_posts_bulk',
    ...(calendar.locationId ? { locationId: calendar.locationId } : {}),
    businessName: business.name,
    ...(calendar.accountIds.length > 0 ? { accountIds: calendar.accountIds } : {}),
    status: calendar.status,
    ...(calendar.creativeStyles.length > 0 ? { creativeStyles: calendar.creativeStyles } : {}),
    ...(Object.keys(calendar.postDefaults).length > 0 ? { postDefaults: calendar.postDefaults } : {}),
    posts: posts.map((post) => ({
      businessName: post.businessName,
      summary: post.summary,
      scheduleDate: post.scheduleDate,
      status: calendar.status,
      creativeStyle: post.creativeStyle,
      creativeStyles: post.creativeStyles
    }))
  };

  return {
    generatedAt: new Date().toISOString(),
    business,
    calendar: {
      postCount: calendar.postCount,
      cadenceDays: calendar.cadenceDays,
      startDate: calendar.startDate,
      timezone: calendar.timezone,
      scheduledHourUtc: calendar.scheduledHourUtc,
      scheduledMinuteUtc: calendar.scheduledMinuteUtc,
      status: calendar.status,
      accountIds: calendar.accountIds,
      creativeStyles: calendar.creativeStyles
    },
    knowledgeBaseMode: business.knowledgeBaseRef ? 'deferred_to_knowledge_base' : 'generic',
    posts,
    mutationRequest
  };
}
