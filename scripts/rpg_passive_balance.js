/* Monte Carlo estimate for the proposed Lv.50 passive designs.
 * This is intentionally independent from the production battle engine: the
 * passives do not exist there yet. It models a 15-round Lv.50/T50 encounter.
 */
'use strict';

const TRIALS = 100000;
const ROUNDS = 15;
const ENEMY_DEFENSE = 110;
const DEFENSE_FACTOR = 0.35;

const stats = {
  infantry: { attack: 576, hp: 1980, crit: 0.45, critDamage: 1.5, stability: [0.8, 1.2] },
  knight: { attack: 392, hp: 2508, crit: 0.45, critDamage: 1.25, stability: [0.8, 1.2] },
  archer: { attack: 480, hp: 1452, crit: 0.72, critDamage: 1.75, stability: [0.8, 1.2] },
  monk: { attack: 392, hp: 1452, healing: 734, crit: 0.61, critDamage: 1.25, stability: [0.8, 1.2] },
};

function rng(seed) {
  return function random() {
    seed |= 0;
    seed = seed + 0x6D2B79F5 | 0;
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

function rollHit(s, power, random, options = {}) {
  const precise = options.precise || false;
  const hit = precise || random() < 0.95;
  if (!hit) return { damage: 0, hit: false, crit: false };
  const crit = random() < s.crit;
  const stability = s.stability[0] + random() * (s.stability[1] - s.stability[0]);
  const defense = options.ignoreDefense ? 0 : ENEMY_DEFENSE * DEFENSE_FACTOR;
  let damage = Math.max(1, s.attack * power - defense) * stability;
  if (crit) damage *= s.critDamage;
  damage *= options.multiplier || 1;
  return { damage, hit: true, crit };
}

function average(simulate) {
  let baseline = 0;
  let passive = 0;
  let secondary = 0;
  let procs = 0;
  for (let seed = 1; seed <= TRIALS; seed++) {
    const result = simulate(rng(seed));
    baseline += result.baseline || 0;
    passive += result.passive || 0;
    secondary += result.secondary || 0;
    procs += result.procs || 0;
  }
  baseline /= TRIALS;
  passive /= TRIALS;
  secondary /= TRIALS;
  procs /= TRIALS;
  return {
    uplift: baseline ? (passive / baseline - 1) * 100 : 0,
    baseline,
    passive,
    secondary,
    procs,
  };
}

function directSequence(s, sequence, passiveLogic) {
  return average(random => {
    let baseline = 0;
    let passive = 0;
    const state = {};
    let procs = 0;
    for (const action of sequence) {
      const powers = action.powers || [];
      const modifier = passiveLogic.before ? passiveLogic.before(state, action) : {};
      if (modifier.proc) procs++;
      for (const power of powers) {
        // Paired random draws keep baseline and passive variance aligned.
        const seedA = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(s, power, rng(seedA));
        const passiveRoll = rollHit(s, power, rng(seedA), modifier);
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
        if (passiveLogic.onHit) passiveLogic.onHit(state, action, passiveRoll);
      }
      if (modifier.extraPower) {
        passive += rollHit(s, modifier.extraPower, random).damage;
      }
      if (passiveLogic.after) passiveLogic.after(state, action, modifier);
    }
    return { baseline, passive, procs };
  });
}

const A = (name, ...powers) => ({ name, powers });
const N = name => ({ name, powers: [] });

function infantryChain() {
  const sequence = [A('break', 1), A('crush', 2.2), A('strike', 1.6), A('basic', 1),
    A('break', 1), A('strike', 1.6), A('crush', 2.2), A('basic', 1),
    A('break', 1), A('strike', 1.6), A('basic', 1), A('basic', 1),
    A('break', 1), A('crush', 2.2), A('strike', 1.6)];
  return directSequence(stats.infantry, sequence, {
    before(state, action) {
      if (action.name === 'basic') return {};
      state.chain = action.name === state.last ? 1 : (state.chain || 0) + 1;
      state.last = action.name;
      if (state.chain >= 3) {
        state.chain = 0;
        return { multiplier: 1.5, proc: true };
      }
      return {};
    },
  });
}

function infantryRotation() {
  // Preparation and damage alternate often; break counts as both and resolves
  // damage before adding its new guard stack.
  const sequence = [N('stance'), A('break', 1), A('crush', 2.2), A('basic', 1),
    N('stance'), A('break', 1), A('crush', 2.2), A('basic', 1),
    N('stance'), A('break', 1), A('crush', 2.2), A('basic', 1),
    N('stance'), A('break', 1), A('crush', 2.2)];
  return average(random => {
    let baseline = 0, passive = 0, guard = 0, offense = 0, healing = 0, procs = 0;
    for (const action of sequence) {
      const isPrep = action.name === 'stance' || action.name === 'break';
      if (isPrep && offense) {
        healing += stats.infantry.hp * 0.03 * offense;
        offense = 0;
      }
      for (const power of action.powers) {
        const paired = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(stats.infantry, power, rng(paired));
        const mult = guard ? 1 + 0.15 * guard : 1;
        const passiveRoll = rollHit(stats.infantry, power, rng(paired), { multiplier: mult });
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
        if (guard) { procs++; guard = 0; }
        offense = Math.min(2, offense + 1);
      }
      if (isPrep) guard = Math.min(2, guard + 1);
    }
    return { baseline, passive, secondary: healing, procs };
  });
}

function infantryBlood(enemyCount = 1, partySize = 4) {
  const sequence = [N('stance'), A('crush', 2.2), A('strike', 1.6), A('basic', 1),
    N('stance'), A('crush', 2.2), A('strike', 1.6), A('basic', 1),
    N('stance'), A('crush', 2.2), A('strike', 1.6), A('basic', 1),
    N('stance'), A('crush', 2.2), A('strike', 1.6)];
  return average(random => {
    let baseline = 0, passive = 0, rage = 0, healing = 0, procs = 0;
    for (const action of sequence) {
      let wasHit = false;
      for (let enemy = 0; enemy < enemyCount; enemy++) {
        if (Math.floor(random() * partySize) === 0) wasHit = true;
      }
      if (wasHit) rage = Math.min(5, rage + 2);
      if (action.name === 'stance') rage = Math.min(5, rage + 1);
      const consume = action.powers.length && rage >= 3 ? rage : 0;
      for (const power of action.powers) {
        const paired = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(stats.infantry, power, rng(paired));
        const passiveRoll = rollHit(stats.infantry, power, rng(paired), { multiplier: consume ? 1.3 : 1 });
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
        if (consume === 5) healing += passiveRoll.damage * 0.15;
      }
      if (consume) { rage = 0; procs++; }
    }
    return { baseline, passive, secondary: healing, procs };
  });
}

function knightRevenge(enemyCount = 1) {
  const sequence = [N('taunt'), A('charge', 0.5), A('bash', 1.2), A('basic', 1),
    N('taunt'), A('charge', 0.5), A('bash', 1.2), A('basic', 1),
    N('taunt'), A('charge', 0.5), A('bash', 1.2), A('basic', 1),
    N('taunt'), A('charge', 0.5), A('bash', 1.2)];
  return average(random => {
    let baseline = 0, passive = 0, revenge = 0, healing = 0, procs = 0;
    let tauntUntil = 0;
    for (let round = 1; round <= ROUNDS; round++) {
      const action = sequence[round - 1];
      if (action.name === 'taunt') tauntUntil = round + 1;
      const consume = action.powers.length ? revenge : 0;
      for (const originalPower of action.powers) {
        const override = action.name === 'charge';
        const s = override ? { ...stats.knight, attack: stats.knight.hp } : stats.knight;
        const paired = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(s, originalPower, rng(paired));
        const passiveRoll = rollHit(s, originalPower, rng(paired), { multiplier: 1 + consume * 0.12 });
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
      }
      if (consume) {
        healing += stats.knight.hp * 0.02 * consume;
        revenge = 0;
        procs++;
      }
      // Multiple enemies may all hit the knight, but vengeance is capped at
      // one layer per round to prevent add encounters from multiplying it.
      if (round <= tauntUntil) revenge = Math.min(3, revenge + 1);
    }
    return { baseline, passive, secondary: healing, procs };
  });
}

function knightGuard(enemyCount = 1, partySize = 4) {
  // Guard is cast on rounds 1/5/9/13 and lasts for its round plus the next.
  return average(random => {
    let watch = 0, healing = 0, baselineIncoming = 0, passiveIncoming = 0, procs = 0;
    let empoweredUntil = 0;
    for (let round = 1; round <= ROUNDS; round++) {
      if ([1, 5, 9, 13].includes(round)) {
        if (watch >= 2) {
          healing += partySize * stats.knight.hp * 0.02;
          empoweredUntil = round + 1;
          procs++;
          watch = 0;
        }
      }
      const guarded = [1, 2, 5, 6, 9, 10, 13, 14].includes(round);
      const seen = new Set();
      for (let enemy = 0; enemy < enemyCount; enemy++) {
        const target = Math.floor(random() * partySize);
        baselineIncoming += 1;
        passiveIncoming += round <= empoweredUntil ? 0.9 : 1;
        if (guarded && target !== 0) seen.add(target);
      }
      watch = Math.min(2, watch + seen.size);
    }
    return { baseline: baselineIncoming, passive: passiveIncoming, secondary: healing, procs };
  });
}

function knightCombo() {
  const sequence = [A('bash', 1.2), N('taunt'), A('charge', 0.5), A('basic', 1), A('basic', 1),
    A('bash', 1.2), N('taunt'), A('charge', 0.5), A('basic', 1), A('basic', 1),
    A('bash', 1.2), N('taunt'), A('charge', 0.5), A('basic', 1), A('basic', 1)];
  return average(random => {
    let baseline = 0, passive = 0, state = null, procs = 0;
    for (const action of sequence) {
      for (const originalPower of action.powers) {
        const override = action.name === 'charge';
        const s = override ? { ...stats.knight, attack: stats.knight.hp } : stats.knight;
        const paired = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(s, originalPower, rng(paired));
        const mult = action.name === 'charge' && state === 'opening' ? 1.25 : 1;
        const passiveRoll = rollHit(s, originalPower, rng(paired), { multiplier: mult });
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
        if (mult > 1) procs++;
        if (action.name === 'bash' && state === 'momentum') {
          passive += rollHit(stats.knight, 0.4, random).damage;
          procs++;
        }
      }
      if (action.name === 'bash') state = 'opening';
      if (action.name === 'charge') state = 'momentum';
    }
    return { baseline, passive, procs };
  });
}

const archerSequence = [A('triple', .85, .85, .85), A('double', .9, .9), A('hinder', 1.2), A('basic', 1),
  A('triple', .85, .85, .85), A('double', .9, .9), A('hinder', 1.2), A('basic', 1),
  A('triple', .85, .85, .85), A('double', .9, .9), A('hinder', 1.2), A('basic', 1),
  A('triple', .85, .85, .85), A('double', .9, .9), A('hinder', 1.2)];

function archerTempo() {
  return directSequence(stats.archer, archerSequence, {
    before() { return {}; },
    onHit(state, action, roll) {
      if (!roll.hit) { state.tempo = 0; return; }
      state.lastBonus = 0.03 * (state.tempo || 0);
      if ((state.tempo || 0) >= 6) state.tempo = 0;
      else state.tempo = (state.tempo || 0) + 1;
    },
  });
}

// The generic paired helper cannot apply a changing multiplier within one skill,
// so tempo is simulated explicitly.
function archerTempoExplicit() {
  return average(random => {
    let baseline = 0, passive = 0, tempo = 0, procs = 0;
    for (const action of archerSequence) {
      for (const power of action.powers) {
        const paired = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(stats.archer, power, rng(paired));
        const bonus = 0.03 * tempo;
        const passiveRoll = rollHit(stats.archer, power, rng(paired), { multiplier: 1 + bonus });
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
        if (!passiveRoll.hit) tempo = 0;
        else if (tempo >= 6) {
          passive += rollHit(stats.archer, 0.8, random).damage;
          tempo = 0;
          procs++;
        } else tempo++;
      }
    }
    return { baseline, passive, procs };
  });
}

function archerPoison() {
  const sequence = [A('poison', 1.1), A('double', .9, .9), A('hinder', 1.2), A('basic', 1),
    A('poison', 1.1), A('double', .9, .9), A('hinder', 1.2), A('basic', 1),
    A('poison', 1.1), A('double', .9, .9), A('hinder', 1.2), A('basic', 1),
    A('poison', 1.1), A('double', .9, .9), A('hinder', 1.2)];
  return average(random => {
    let baseline = 0, passive = 0, toxicity = 0, procs = 0;
    const ticks = [];
    for (let round = 1; round <= ROUNDS; round++) {
      for (const tick of ticks.filter(t => t.round === round)) {
        baseline += stats.archer.attack * 0.7;
        passive += stats.archer.attack * 0.7;
        toxicity++;
      }
      const action = sequence[round - 1];
      for (const power of action.powers) {
        const paired = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(stats.archer, power, rng(paired));
        const passiveRoll = rollHit(stats.archer, power, rng(paired));
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
        if (passiveRoll.hit && toxicity >= 3) {
          passive += stats.archer.attack * 1.8;
          toxicity -= 3;
          procs++;
        }
        if (action.name === 'poison' && passiveRoll.hit) {
          ticks.push({ round: round + 1 }, { round: round + 2 });
        }
      }
    }
    return { baseline, passive, procs };
  });
}

function archerInsight() {
  return average(random => {
    let baseline = 0, passive = 0, insight = 0, procs = 0;
    for (const action of archerSequence) {
      const empowered = insight >= 3;
      if (empowered) { insight = 0; procs++; }
      for (const power of action.powers) {
        const paired = Math.floor(random() * 0xFFFFFFFF);
        const baseRoll = rollHit(stats.archer, power, rng(paired));
        const passiveRoll = rollHit(stats.archer, power, rng(paired), {
          precise: empowered, multiplier: empowered ? 1.4 : 1,
        });
        baseline += baseRoll.damage;
        passive += passiveRoll.damage;
        if (!empowered && passiveRoll.hit && passiveRoll.crit) insight = Math.min(3, insight + 1);
      }
    }
    return { baseline, passive, procs };
  });
}

function monkGrace() {
  const casts = 8;
  const baseline = casts * stats.monk.healing;
  const procs = Math.floor(casts / 4);
  const extraPerProc = stats.monk.healing * (0.2 + 3 * 0.15);
  return { uplift: procs * extraPerProc / baseline * 100, baseline,
    passive: baseline + procs * extraPerProc, secondary: procs * extraPerProc, procs };
}

function monkHymn() {
  const procs = 3;
  const baselineHealing = 8 * stats.monk.healing;
  const extraHealing = procs * 4 * stats.monk.healing * 0.1;
  return { uplift: extraHealing / baselineHealing * 100, baseline: baselineHealing,
    passive: baselineHealing + extraHealing, secondary: 10 * procs / ROUNDS, procs };
}

function monkCycle() {
  // Strict alternating damage/heal: every action after the first consumes the
  // opposite resource and then creates one resource of its own type.
  const actions = Array.from({ length: ROUNDS }, (_, i) => i % 2 === 0 ? 'damage' : 'heal');
  let baseline = 0, passive = 0, radiance = 0, discipline = 0, procs = 0;
  for (const action of actions) {
    const value = action === 'damage' ? stats.monk.attack : stats.monk.healing;
    const stacks = action === 'damage' ? discipline : radiance;
    baseline += value;
    passive += value * (1 + stacks * 0.15);
    if (stacks) procs++;
    if (action === 'damage') { discipline = 0; radiance = Math.min(3, radiance + 1); }
    else { radiance = 0; discipline = Math.min(3, discipline + 1); }
  }
  return { uplift: (passive / baseline - 1) * 100, baseline, passive, secondary: 0, procs };
}

function pct(value) { return `${value.toFixed(1)}%`; }
function num(value) { return Math.round(value).toLocaleString('en-US'); }

const main = [
  ['裝甲步兵', '百鍊連式', infantryChain(), '額外破甲未計'],
  ['裝甲步兵', '攻守輪轉', infantryRotation(), 'secondary=自療'],
  ['裝甲步兵', '浴血戰意', infantryBlood(), 'secondary=吸血'],
  ['騎士', '復仇誓約', knightRevenge(), 'secondary=自療'],
  ['騎士', '守望誓約', knightGuard(), 'uplift為承傷變化；secondary=團隊治療'],
  ['騎士', '槍盾連攜', knightCombo(), ''],
  ['弓兵', '無間箭勢', archerTempoExplicit(), ''],
  ['弓兵', '猛毒調律', archerPoison(), ''],
  ['弓兵', '弱點觀測', archerInsight(), ''],
  ['僧侶', '恩典回響', monkGrace(), '理論治療，最多回響3人'],
  ['僧侶', '三重聖歌', monkHymn(), 'secondary=平均團隊攻擊增幅%'],
  ['僧侶', '光明輪轉', monkCycle(), '傷害/治療交替'],
];

console.log(`Lv50 passive estimate: ${TRIALS.toLocaleString()} trials, ${ROUNDS} rounds, 4-player single boss`);
console.log('職業\t被動\t主效益\t平均觸發\t次要效益\t備註');
for (const [job, name, result, note] of main) {
  console.log([job, name, pct(result.uplift), result.procs.toFixed(2), num(result.secondary), note].join('\t'));
}

console.log('\nIncoming-hit sensitivity');
console.log('場景\t浴血傷害\t浴血觸發\t復仇傷害\t復仇自療\t守望承傷\t守望團療');
for (const [label, enemies, party] of [['四人單王', 1, 4], ['四人三敵', 3, 4], ['單人單王', 1, 1]]) {
  const blood = infantryBlood(enemies, party);
  const revenge = knightRevenge(enemies);
  const guard = knightGuard(enemies, party);
  console.log([label, pct(blood.uplift), blood.procs.toFixed(2), pct(revenge.uplift), num(revenge.secondary),
    pct(guard.uplift), num(guard.secondary)].join('\t'));
}
