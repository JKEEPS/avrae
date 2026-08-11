import itertools
import json
from collections import namedtuple
from typing import Iterable, List, Optional, TYPE_CHECKING, Union

import disnake

from cogs5e.initiative import InitiativeEffect
from cogs5e.initiative.types import BaseCombatant
from cogs5e.models import embeds
from cogs5e.models.automation.results import AttackResult, DamageResult, TargetIteration
from cogs5e.models.errors import InvalidArgument, InvalidSpellLevel, RequiresLicense
from cogs5e.models.sheet.action import Action, Actions
from cogs5e.models.sheet.attack import Attack, AttackList
from gamedata import lookuputils, monster
from utils import constants
from utils.enums import ActivationType, CritDamageType
from utils.functions import a_or_an, confirm, maybe_http_url, natural_join, search_and_select, smart_trim, verbose_stat
from utils.settings import ServerSettings

if TYPE_CHECKING:
    from cogs5e.initiative import Combatant, Combat
    from cogs5e.models.automation import AutomationResult, Automation
    from cogs5e.models.character import Character
    from cogs5e.models.sheet.statblock import StatBlock
    from gamedata import Spell
    from utils.argparser import ParsedArguments
    from utils.context import AvraeContext


async def run_attack(
    ctx: "AvraeContext",
    embed: disnake.Embed,
    args: "ParsedArguments",
    caster: "StatBlock",
    attack: "Attack",
    targets: List[Union[str, "StatBlock"]],
    combat: Optional["Combat"],
) -> "AutomationResult":
    """
    Runs an attack: adds title, handles -f and -thumb args, commits combat, runs automation, edits embed.
    """
    ieffect = getattr(attack, "__run_automation_kwargs__", {}).get("ieffect")
    is_hidden = args.last("h", type_=bool) or (ieffect and ieffect.hidden)
    if not is_hidden:
        name = caster.get_title_name()
    else:
        name = "An unknown creature"

    if not attack.proper:
        attack_name = a_or_an(attack.name)
    else:
        attack_name = attack.name

    verb = attack.verb or "attacks with"

    if args.last("title") is not None:
        embed.title = args.last("title").replace("[name]", name).replace("[aname]", attack_name).replace("[verb]", verb)
    else:
        embed.title = f"{name} {verb} {attack_name}!"

    # arg overrides (#1163)
    arg_defaults = {
        "criton": attack.criton,
        "phrase": attack.phrase,
        "thumb": attack.thumb,
        "c": attack.extra_crit_damage,
    }
    args.update_nx(arg_defaults)

    trigger_reactions = combat is not None and attack.activation_type != ActivationType.REACTION
    result = await run_automation(
        ctx,
        embed,
        args,
        caster,
        attack.automation,
        targets,
        combat,
        **attack.__run_automation_kwargs__,
        trigger_reactions=trigger_reactions,
    )

    # common embed operations
    embeds.add_fields_from_args(embed, args.get("f"))
    if "thumb" in args:
        embed.set_thumbnail(url=maybe_http_url(args.last("thumb", "")))

    return result


async def run_action(
    ctx: "AvraeContext",
    embed: disnake.Embed,
    args: "ParsedArguments",
    caster: "Character",
    action: "Action",
    targets: List[Union[str, "StatBlock"]],
    combat: Optional["Combat"],
) -> Optional["AutomationResult"]:
    """
    Runs an action: adds title, handles -f and -thumb args, commits combat, runs automation, edits embed.
    """
    await ctx.trigger_typing()

    # entitlements: ensure runner has access to grantor entity
    source_feature = action.gamedata.source_feature
    available_entity_e10s = await ctx.bot.ddb.get_accessible_entities(
        ctx, ctx.author.id, source_feature.entitlement_entity_type
    )
    if not lookuputils.can_access(source_feature, available_entity_e10s):
        raise RequiresLicense(source_feature, available_entity_e10s is not None)

    if not args.last("h", type_=bool):
        name = caster.get_title_name()
    else:
        name = "An unknown creature"

    verb = args.last("verb", "uses")

    if args.last("title") is not None:
        embed.title = args.last("title").replace("[name]", name).replace("[aname]", action.name).replace("[verb]", verb)
    else:
        embed.title = f"{name} uses {action.name}!"

    if action.automation:
        trigger_reactions = combat is not None and action.activation_type != ActivationType.REACTION
        result = await run_automation(
            ctx,
            embed,
            args,
            caster,
            action.automation,
            targets,
            combat,
            trigger_reactions=trigger_reactions,
        )
    else:
        # else, show action description and note that it can't be automated
        if action.snippet:
            embed.description = action.snippet
        else:
            embed.description = "Unknown action effect."
        embed.set_footer(text="No action automation found.")
        result = None

    # common embed operations
    embeds.add_fields_from_args(embed, args.get("f"))
    if "thumb" in args:
        embed.set_thumbnail(url=maybe_http_url(args.last("thumb", "")))

    return result


async def cast_spell(
    spell: "Spell",
    ctx: "AvraeContext",
    caster: "StatBlock",
    targets: List[Union[str, "StatBlock"]],
    args: "ParsedArguments",
    combat: Optional["Combat"] = None,
) -> "CastResult":
    """
    Casts this spell.

    :param spell: The spell to cast.
    :param ctx: The context of the casting.
    :param caster: The caster of this spell.
    :param targets: A list of targets
    :param args: Args
    :param combat: The combat the spell was cast in, if applicable.
    """

    # generic args
    cast_level = args.last("l", spell.level, int)
    ignore = args.last("i", type_=bool)
    title = args.last("title")
    nopact = args.last("nopact", type_=bool)

    # meta checks
    if not spell.level <= cast_level <= 9:
        raise InvalidSpellLevel()

    # caster spell-specific overrides
    dc_override = None
    ab_override = None
    spell_override = None
    is_prepared = True
    spellbook_spell = caster.spellbook.get_spell(spell)
    if spellbook_spell is not None:
        dc_override = spellbook_spell.dc
        ab_override = spellbook_spell.sab
        spell_override = spellbook_spell.mod
        is_prepared = spellbook_spell.prepared

    if not ignore:
        # if I'm a warlock, and I didn't have any slots of this level anyway (#655)
        # automatically scale up to our pact slot level (or the next available level s.t. max > 0)
        if (
            cast_level > 0
            and cast_level == spell.level
            and not caster.spellbook.get_max_slots(cast_level)
            and not caster.spellbook.can_cast(spell, cast_level)
        ):
            if caster.spellbook.pact_slot_level is not None:
                cast_level = caster.spellbook.pact_slot_level
            else:
                cast_level = next(
                    (sl for sl in range(cast_level, 6) if caster.spellbook.get_max_slots(sl)), cast_level
                )  # only scale up to l5
            args["l"] = cast_level

        # can I cast this spell?
        if not caster.spellbook.can_cast(spell, cast_level):
            embed = embeds.EmbedWithAuthor(ctx)
            embed.title = "Cannot cast spell!"
            if not caster.spellbook.get_slots(cast_level):
                # out of spell slots
                err = (
                    f"You don't have enough level {cast_level} slots left! Use `-l <level>` to cast at a different "
                    f"level, `{ctx.prefix}g lr` to take a long rest, or `-i` to ignore spell slots!"
                )
            elif spell.name not in caster.spellbook:
                # don't know spell
                err = (
                    f"You don't know this spell! Use `{ctx.prefix}sb add {spell.name}` to add it to your "
                    "spellbook, or pass `-i` to ignore restrictions."
                )
            else:
                # ?
                err = (
                    "Not enough spell slots remaining, or spell not in known spell list!\n"
                    f"Use `{ctx.prefix}game longrest` to restore all spell slots if this is a character, "
                    "or pass `-i` to ignore restrictions."
                )
            embed.description = err
            if cast_level > 0:
                embed.add_field(name="Spell Slots", value=caster.spellbook.remaining_casts_of(spell, cast_level))
            return CastResult(embed=embed, success=False, automation_result=None)

        # #1000: is this spell prepared (soft check)?
        if not is_prepared:
            skip_prep_conf = await confirm(
                ctx,
                f"{spell.name} is not prepared. Do you want to cast it anyway? (Reply with yes/no)",
                delete_msgs=True,
            )
            if not skip_prep_conf:
                embed = embeds.EmbedWithAuthor(
                    ctx,
                    title=f"Cannot cast spell!",
                    description=(
                        f"{spell.name} is not prepared! Prepare it on your character sheet and use "
                        f"`{ctx.prefix}update` to mark it as prepared, or use `-i` to ignore restrictions."
                    ),
                )
                return CastResult(embed=embed, success=False, automation_result=None)

        # use resource
        caster.spellbook.cast(spell, cast_level, pact=not nopact)

    # base stat stuff
    mod_arg = args.last("mod", type_=int)
    with_arg = args.last("with")
    stat_override = ""
    if mod_arg is not None:
        mod = mod_arg
        prof_bonus = caster.stats.prof_bonus
        dc_override = 8 + mod + prof_bonus
        ab_override = mod + prof_bonus
        spell_override = mod
    elif with_arg is not None:
        abbr_with = with_arg[:3].lower()
        if abbr_with not in constants.STAT_ABBREVIATIONS:
            raise InvalidArgument(f"{with_arg} is not a valid stat to cast with.")
        with_arg = abbr_with
        mod = caster.stats.get_mod(with_arg)
        dc_override = 8 + mod + caster.stats.prof_bonus
        ab_override = mod + caster.stats.prof_bonus
        spell_override = mod
        stat_override = f" with {verbose_stat(with_arg)}"

    # begin setup
    embed = disnake.Embed()
    if title:
        embed.title = (
            title.replace("[name]", caster.name)
            .replace("[aname]", spell.name)
            .replace("[sname]", spell.name)
            .replace("[verb]", "casts")
        )  # #1514, [aname] is action name now, #1587, add verb to action/cast
    else:
        embed.title = f"{caster.get_title_name()} casts {spell.name}{stat_override}!"
    if targets is None:
        targets = [None]

    # concentration
    noconc = args.last("noconc", type_=bool)
    conc_conflict = None
    conc_effect = None
    if all((spell.concentration, isinstance(caster, BaseCombatant), combat, not noconc)):
        caster: "Combatant"  # to make pycharm typechecking happy
        duration = args.last("dur", spell.get_combat_duration(), int)
        conc_effect = InitiativeEffect.new(
            combat=combat, combatant=caster, name=spell.name, duration=duration, concentration=True
        )
        # noinspection PyUnresolvedReferences
        effect_result = caster.add_effect(conc_effect)
        conc_conflict = effect_result["conc_conflict"]

    # run
    automation_result = None
    if spell.automation and spell.automation.effects:
        trigger_reactions = combat is not None
        if spell.time and "reaction" in spell.time.lower():
            trigger_reactions = False
        automation_result = await run_automation(
            ctx,
            embed,
            args,
            caster,
            spell.automation,
            targets,
            combat,
            always_commit_caster=True,
            trigger_reactions=trigger_reactions,
            spell=spell,
            conc_effect=conc_effect,
            ab_override=ab_override,
            dc_override=dc_override,
            spell_override=spell_override,
            spell_level_override=cast_level,
        )
    else:
        # no automation, display spell description
        phrase = args.join("phrase", "\n")
        if phrase:
            embed.description = f"*{phrase}*"
        embed.add_field(name="Meta", value=f"**Range**: {spell.range}")
        embed.add_field(name="Description", value=smart_trim(spell.description), inline=False)
        embed.set_footer(text="No spell automation found.")

        # commit the caster
        if combat:
            await combat.final(ctx)
        elif hasattr(caster, "commit"):
            await caster.commit(ctx)

    if cast_level != spell.level and spell.higherlevels:
        embed.add_field(name="At Higher Levels", value=smart_trim(spell.higherlevels), inline=False)

    if cast_level > 0 and not ignore:
        remaining_casts = caster.spellbook.remaining_casts_of(spell, cast_level)
        if not (isinstance(caster.spellbook, monster.MonsterSpellbook) and spell.name in caster.spellbook.at_will):
            remaining_casts += " (-1)"
        embed.add_field(name="Spell Slots", value=remaining_casts)

    if conc_conflict:
        conflicts = ", ".join(e.name for e in conc_conflict)
        embed.add_field(name="Concentration", value=f"Dropped {conflicts} due to concentration.")

    # embed operations
    if "thumb" in args:
        embed.set_thumbnail(url=maybe_http_url(args.last("thumb", "")))
    elif spell.image:
        embed.set_thumbnail(url=spell.image)
    embeds.add_fields_from_args(embed, args.get("f"))
    lookuputils.handle_source_footer(embed, spell, add_source_str=False)

    return CastResult(embed=embed, success=True, automation_result=automation_result)


CastResult = namedtuple("CastResult", "embed success automation_result")
REACTION_PENDING_TTL = 60 * 60


def _collect_reaction_triggers(automation_result: "AutomationResult") -> dict[str, list[dict[str, int | bool]]]:
    triggers: dict[str, list[dict[str, int | bool]]] = {}

    def walk(node):
        if isinstance(node, TargetIteration):
            target_id = node.target_id
            if not target_id:
                return

            saw_attack = False
            damage_sum = 0
            target_triggers: list[dict[str, int | bool]] = []

            def walk_target(child):
                nonlocal saw_attack, damage_sum
                if isinstance(child, AttackResult):
                    saw_attack = True
                    if child.did_hit:
                        target_triggers.append({
                            "hit": True,
                            "damage": child.get_damage(),
                        })
                    return
                if isinstance(child, DamageResult):
                    if child.damage > 0:
                        damage_sum += child.damage
                    return
                for grandchild in child.get_children():
                    walk_target(grandchild)

            for child in node.results:
                walk_target(child)

            if not saw_attack and damage_sum:
                target_triggers.append({"hit": False, "damage": damage_sum})

            if target_triggers:
                triggers.setdefault(target_id, []).extend(target_triggers)
            return

        for child in node.get_children():
            walk(child)

    walk(automation_result)
    return triggers


def _reaction_names_for_target(target: "BaseCombatant") -> list[str]:
    names: list[str] = []
    attacks = getattr(target, "attacks", None)
    if attacks is not None:
        names.extend(a.name for a in attacks.reactions)

    character = getattr(target, "character", None)
    if character is not None:
        names.extend(a.name for a in character.actions.reactions)

    seen = set()
    unique = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def _reaction_pending_key(combat: "Combat", combatant_id: str) -> str:
    return f"cog.reaction.pending.{combat.channel_id}.{combatant_id}"


async def _load_pending_reactions(ctx: "AvraeContext", combat: "Combat", combatant_id: str) -> dict | None:
    key = _reaction_pending_key(combat, combatant_id)
    raw = await ctx.bot.rdb.get(key)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _save_pending_reactions(ctx: "AvraeContext", combat: "Combat", combatant_id: str, data: dict) -> None:
    key = _reaction_pending_key(combat, combatant_id)
    await ctx.bot.rdb.setex(key, json.dumps(data), REACTION_PENDING_TTL)


async def _delete_prompt_message(ctx: "AvraeContext", prompt: dict | None) -> None:
    if not prompt:
        return
    channel_id = prompt.get("channel_id")
    message_id = prompt.get("message_id")
    if channel_id is None or message_id is None:
        return

    channel = ctx.bot.get_channel(int(channel_id))
    if channel is None and hasattr(ctx.bot, "fetch_channel"):
        try:
            channel = await ctx.bot.fetch_channel(int(channel_id))
        except disnake.HTTPException:
            return
        except Exception:
            return

    if channel is None:
        return

    try:
        msg = disnake.PartialMessage(channel=channel, id=int(message_id))
        await msg.delete()
    except disnake.HTTPException:
        pass


async def clear_pending_reactions(
    ctx: "AvraeContext",
    combat: "Combat",
    combatant_id: str,
    *,
    delete_prompt: bool = True,
) -> None:
    if delete_prompt:
        pending = await _load_pending_reactions(ctx, combat, combatant_id)
        if pending:
            await _delete_prompt_message(ctx, pending.get("prompt"))

    key = _reaction_pending_key(combat, combatant_id)
    await ctx.bot.rdb.delete(key)


def reaction_used_this_round(combat: "Combat", combatant_id: str) -> bool:
    meta = combat.metadata or {}
    used = meta.get("reaction_used_rounds", {})
    return used.get(str(combatant_id)) == combat.round_num


def mark_reaction_used(combat: "Combat", combatant_id: str) -> None:
    if combat.metadata is None:
        combat.metadata = {}
    used = combat.metadata.setdefault("reaction_used_rounds", {})
    used[str(combatant_id)] = combat.round_num


def _format_trigger_line(trigger: dict) -> str:
    parts = []
    attacker_name = trigger.get("attacker_name")
    if attacker_name:
        parts.append(f"from {attacker_name}")
    if trigger.get("hit"):
        parts.append("hit")
    damage = trigger.get("damage")
    if damage:
        parts.append(f"{damage} damage")
    if not parts:
        return "triggered"
    return " ".join(parts)


def _reaction_buttons(
    channel_id: int,
    combatant_id: str,
    trigger_index: int,
    reaction_names: list[str],
    selected: bool,
) -> list[disnake.ui.Button]:
    buttons = []
    action = "use_sel" if selected else "use"
    for idx, name in enumerate(reaction_names[:25]):
        label = smart_trim(name, max_len=80, dots="...")
        custom_id = (
            f"{constants.B_REACTION_PROMPT}{action}:{channel_id}:{combatant_id}:{trigger_index}:{idx}"
        )
        buttons.append(disnake.ui.Button(label=label, style=disnake.ButtonStyle.primary, custom_id=custom_id))
    return buttons


def _number_buttons(channel_id: int, combatant_id: str, count: int) -> list[disnake.ui.Button]:
    buttons = []
    for idx in range(min(count, 25)):
        custom_id = f"{constants.B_REACTION_PROMPT}pick:{channel_id}:{combatant_id}:{idx}"
        buttons.append(disnake.ui.Button(label=str(idx + 1), style=disnake.ButtonStyle.secondary, custom_id=custom_id))
    return buttons


async def maybe_prompt_reactions(
    ctx: "AvraeContext",
    combat: Optional["Combat"],
    caster: "StatBlock",
    automation_result: Optional["AutomationResult"],
) -> None:
    if combat is None or automation_result is None:
        return

    triggers = _collect_reaction_triggers(automation_result)
    if not triggers:
        return

    attacker_id = getattr(caster, "id", None)
    attacker_name = caster.get_title_name() if caster is not None else None

    for target_id, new_triggers in triggers.items():
        target = combat.combatant_by_id(target_id)
        if target is None:
            continue

        if reaction_used_this_round(combat, target_id):
            continue

        reaction_names = _reaction_names_for_target(target)
        if not reaction_names:
            continue

        pending = await _load_pending_reactions(ctx, combat, target_id)
        existing_prompt = pending.get("prompt") if pending else None
        if not pending or pending.get("round") != combat.round_num:
            pending = {"round": combat.round_num, "triggers": [], "reactions": reaction_names}
        else:
            pending["reactions"] = reaction_names

        for trigger in new_triggers:
            if not trigger.get("hit") and not trigger.get("damage"):
                continue
            pending["triggers"].append({
                "attacker_id": attacker_id,
                "attacker_name": attacker_name,
                "hit": trigger.get("hit", False),
                "damage": trigger.get("damage", 0),
            })

        if not pending["triggers"]:
            continue

        channel_id = combat.channel_id
        message = None
        if len(pending["triggers"]) > 1:
            display_triggers = pending["triggers"][:25]
            lines = [f"{idx + 1}) {_format_trigger_line(t)}" for idx, t in enumerate(display_triggers)]
            prompt = (
                f"Multiple reaction triggers for {target.get_title_name()}.\n"
                "Choose which to react to:\n"
                + "\n".join(lines)
            )
            if len(pending["triggers"]) > len(display_triggers):
                prompt += f"\n...and {len(pending['triggers']) - len(display_triggers)} more."
            buttons = _number_buttons(channel_id, target_id, len(display_triggers))
            await _delete_prompt_message(ctx, existing_prompt)
            if getattr(target, "is_private", False):
                message = await target.message_controller(ctx, prompt, components=buttons)
            else:
                message = await ctx.send(prompt, components=buttons)
        else:
            trigger = pending["triggers"][0]
            line = _format_trigger_line(trigger)
            reaction_list = smart_trim(natural_join(reaction_names, "or"), max_len=400)
            prompt = f"{target.get_title_name()} can react ({line}). {reaction_list}"
            buttons = _reaction_buttons(channel_id, target_id, 0, reaction_names, selected=False)
            await _delete_prompt_message(ctx, existing_prompt)
            if getattr(target, "is_private", False):
                message = await target.message_controller(ctx, prompt, components=buttons)
            else:
                message = await ctx.send(prompt, components=buttons)

        if message is not None:
            pending["prompt"] = {"channel_id": message.channel.id, "message_id": message.id}
        await _save_pending_reactions(ctx, combat, target_id, pending)


async def run_automation(
    ctx: Union["AvraeContext", disnake.Interaction],
    embed: disnake.Embed,
    args: "ParsedArguments",
    caster: "StatBlock",
    automation: "Automation",
    targets: List[Union[str, "StatBlock"]],
    combat: Optional["Combat"],
    always_commit_caster: bool = False,
    trigger_reactions: bool = True,
    **kwargs,
) -> "AutomationResult":
    """
    Common automation runner
    """
    # get crit type in context
    # this could be running from an Interaction, which doesn't have the get_server_settings utility;
    # for now we load from AvraeContext if it's available, otherwise we get it from the db directly
    if hasattr(ctx, "get_server_settings"):
        guild_settings = await ctx.get_server_settings()
    else:
        guild_settings = await ServerSettings.for_guild(mdb=ctx.bot.mdb, guild_id=ctx.guild.id)
    if guild_settings:
        crit_type = guild_settings.crit_type
    else:
        crit_type = CritDamageType.NORMAL

    # run the automation
    result = await automation.run(
        ctx, embed, caster, targets, args, combat=combat, title=embed.title, crit_type=crit_type, **kwargs
    )

    # do commits
    if combat:
        # NLP: record the automation run
        if (nlp_recorder := combat.nlp_recorder) is not None:
            await nlp_recorder.on_automation_run(ctx, combat, automation, result, caster, targets)
        await combat.final(ctx)
    # commit character only if we have not already committed it via combat final
    if (
        (result.caster_needs_commit or always_commit_caster)
        and hasattr(caster, "commit")
        and not (combat and caster in combat.get_combatants())
    ):
        await caster.commit(ctx)

    if trigger_reactions and combat is not None and not kwargs.get("from_button"):
        await maybe_prompt_reactions(ctx, combat, caster, result)

    return result


async def select_action(
    ctx: "AvraeContext",
    name: str,
    attacks: "AttackList",
    actions: "Actions" = None,
    allow_no_automation: bool = False,
    **kwargs,
) -> Union["Attack", "Action"]:
    """
    Prompts the user to select an action from the caster's valid list of runnable actions, or returns a single
    unambiguous action.

    :param allow_no_automation:
        When selecting from a player's action list, whether to allow returning an action that has no action gamedata.
    :rtype: cogs5e.models.sheet.attack.Attack or cogs5e.models.sheet.action.Action
    """
    if actions is None:
        actions = []
    elif not allow_no_automation:
        actions = filter(lambda action: action.automation is not None, actions)

    return await search_and_select(
        ctx, list_to_search=list(itertools.chain(attacks, actions)), query=name, key=lambda a: a.name, **kwargs
    )


# ==== action display ====
async def send_action_list(
    ctx: "AvraeContext",
    caster: "StatBlock",
    destination: disnake.abc.Messageable = None,
    attacks: "AttackList" = None,
    actions: "Actions" = None,
    embed: disnake.Embed = None,
    args: Iterable[str] = None,
):
    """
    Sends the list of actions and attacks given to the given destination.
    """
    if destination is None:
        destination = ctx
    if attacks is None:
        attacks = AttackList()
    if actions is None:
        actions = Actions()
    if embed is None:
        embed = disnake.Embed(color=caster.get_color(), title=f"{caster.get_title_name()}'s Actions")
    if args is None:
        args = ()

    await destination.trigger_typing()

    # long embed builder
    ep = embeds.EmbedPaginator(embed)
    fields = []

    # arg setup
    verbose = "-v" in args
    display_attacks = "attack" in args
    display_actions = "action" in args
    display_bonus = "bonus" in args
    display_reactions = "reaction" in args
    display_other = "other" in args
    display_legendary = "legendary" in args
    display_mythic = "mythic" in args
    display_lair = "lair" in args
    is_display_filtered = any((
        display_attacks,
        display_actions,
        display_bonus,
        display_reactions,
        display_other,
        display_legendary,
        display_mythic,
        display_lair,
    ))
    filtered_action_type_strs = list(
        itertools.compress(
            (
                "attacks",
                "actions",
                "bonus actions",
                "reactions",
                "other actions",
                "legendary actions",
                "mythic actions",
                "lair actions",
            ),
            (
                display_attacks,
                display_actions,
                display_bonus,
                display_reactions,
                display_other,
                display_legendary,
                display_mythic,
                display_lair,
            ),
        )
    )

    # helpers
    non_automated_count = 0
    non_e10s_count = 0
    e10s_map = {}
    source_names = set()

    # since the sheet displays the description regardless of entitlements, we do here too
    async def add_action_field(title, action_source, attack_source=None):
        nonlocal non_automated_count, non_e10s_count  # eh
        action_texts = []

        # #1833 display attacks with activation types
        if attack_source:
            action_texts.append(attack_source.build_str(caster))

        # display actions
        for action in sorted(action_source, key=lambda a: a.name):
            has_automation = action.gamedata is not None
            has_e10s = True
            # entitlement stuff
            if has_automation:
                source_feature = action.gamedata.source_feature
                if source_feature.entitlement_entity_type not in e10s_map:
                    e10s_map[source_feature.entitlement_entity_type] = await ctx.bot.ddb.get_accessible_entities(
                        ctx, ctx.author.id, source_feature.entitlement_entity_type
                    )
                source_feature_type_e10s = e10s_map[source_feature.entitlement_entity_type]
                has_e10s = lookuputils.can_access(source_feature, source_feature_type_e10s)
                if not has_e10s:
                    non_e10s_count += 1
                    source_names.add(source_feature.source)

            if verbose:
                name = f"**{action.name}**" if has_automation and has_e10s else f"***{action.name}***"
                action_texts.append(f"{name}: {action.build_str(caster=caster, snippet=True)}")
            elif has_automation:
                name = f"**{action.name}**" if has_e10s else f"***{action.name}***"
                action_texts.append(f"**{name}**: {action.build_str(caster=caster, snippet=False)}")

            # count these for extra display
            if not has_automation:
                non_automated_count += 1
        if not action_texts:
            return
        action_text = "\n".join(action_texts)
        fields.append({"name": title, "value": action_text})

    # display
    if display_attacks or not is_display_filtered:
        atk_str = attacks.no_activation_types.build_str(caster)
        fields.append({"name": "Attacks", "value": atk_str})
    if display_actions or not is_display_filtered:
        await add_action_field("Actions", actions.full_actions, attacks.full_actions)
    if display_bonus or not is_display_filtered:
        await add_action_field("Bonus Actions", actions.bonus_actions, attacks.bonus_actions)
    if display_reactions or not is_display_filtered:
        await add_action_field("Reactions", actions.reactions, attacks.reactions)
    if display_other or not is_display_filtered:
        await add_action_field("Other", actions.other_actions, attacks.other_attacks)
    if display_legendary or not is_display_filtered:
        await add_action_field("Legendary Actions", actions.legendary_actions, attacks.legendary_actions)
    if display_mythic or not is_display_filtered:
        await add_action_field("Mythic Actions", actions.mythic_actions, attacks.mythic_actions)
    if display_lair or not is_display_filtered:
        await add_action_field("Lair Actions", actions.lair_actions, attacks.lair_actions)

    # build embed

    # description: filtering help
    description = ""
    if not fields:
        if is_display_filtered:
            description = f"{caster.get_title_name()} has no {natural_join(filtered_action_type_strs, 'or')}."
        else:
            description = f"{caster.get_title_name()} has no actions."
    elif is_display_filtered:
        description = f"Only displaying {natural_join(filtered_action_type_strs, 'and')}."

    # description: entitlements help
    if not verbose and actions and non_e10s_count:
        has_ddb_link = any(v is not None for v in e10s_map.values())
        if not has_ddb_link:
            description = (
                f"{description}\nItalicized actions are for display only and cannot be run. "
                "Connect your D&D Beyond account to unlock the full potential of these actions!"
            )
        else:
            source_names = natural_join([lookuputils.long_source_name(s) for s in source_names], "and")
            description = (
                f"{description}\nItalicized actions are for display only and cannot be run. Unlock "
                f"{source_names} on your D&D Beyond account to unlock the full potential of these actions!"
            )
    if description:
        ep.add_description(description.strip())

    # fields
    for field in fields:
        ep.add_field(**field)

    # footer
    if not verbose and actions:
        if non_automated_count:
            ep.set_footer(
                value=(
                    "Use the -v argument to view each action's full description "
                    f"and {non_automated_count} display-only actions."
                )
            )
        else:
            ep.set_footer(value=f"Use the -v argument to view each action's full description.")
    elif verbose and (non_automated_count or non_e10s_count):
        ep.set_footer(value="Italicized actions are for display only and cannot be run.")

    await ep.send_to(destination)
