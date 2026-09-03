#!/usr/bin/env python3
"""Passerelle Liebherr SmartDevice HomeAPI <-> MQTT Discovery.

Publie les états et les commandes d'un appareil de froid Liebherr sur MQTT au
format « MQTT Discovery » (dit aussi « HA Discovery »), que le plugin Jeedom
MQTT Discovery transforme automatiquement en équipement et en commandes.

Le dialogue avec Liebherr passe par `pyliebherrhomeapi`, la bibliothèque de
l'intégration officielle Home Assistant. Les états arrivent en **push** par
Server-Sent Events : aucun polling permanent de l'API cloud, ce qui évite les
limitations de débit imposées par Liebherr.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import re
import signal
import sys
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import paho.mqtt.client as mqtt
from pyliebherrhomeapi import (
    AutoDoorControl,
    BioFreshPlusControl,
    BioFreshPlusMode,
    Device,
    HydroBreezeControl,
    HydroBreezeMode,
    IceMakerControl,
    IceMakerMode,
    LiebherrAuthenticationError,
    LiebherrClient,
    LiebherrError,
    LiebherrNotFoundError,
    LiebherrPreconditionFailedError,
    PresentationLightControl,
    TemperatureControl,
    TemperatureUnit,
    ToggleControl,
)

LOGGER = logging.getLogger("liebherr2mqtt")

CONFIG_PATH = "/config/liebherr2mqtt.conf"

# Libellés français des contrôles connus de la HomeAPI. Un contrôle absent de
# cette table reste exposé, sous son nom brut : mieux vaut une commande au nom
# technique qu'une donnée perdue si Liebherr en ajoute un.
CONTROL_LABELS = {
    "temperature": "Température",
    "supercool": "SuperCool",
    "superfrost": "SuperFrost",
    "partymode": "Mode Party",
    "nightmode": "Mode Nuit",
    "holidaymode": "Mode Vacances",
    "bottletimer": "Minuteur bouteille",
    "icemaker": "Machine à glaçons",
    "hydrobreeze": "HydroBreeze",
    "biofreshplus": "BioFresh-Plus",
    "presentationlight": "Éclairage",
    "autodoor": "Porte automatique",
}

ZONE_LABELS = {"top": "haut", "middle": "milieu", "bottom": "bas"}

ICE_MAKER_LABELS = {"off": "Arrêt", "on": "Marche", "max_ice": "MaxIce"}
HYDRO_BREEZE_LABELS = {
    "off": "Arrêt",
    "low": "Faible",
    "medium": "Moyen",
    "high": "Fort",
}
BIOFRESH_PLUS_LABELS = {
    "zero_zero": "0 °C / 0 °C",
    "zero_minus_two": "0 °C / -2 °C",
    "minus_two_minus_two": "-2 °C / -2 °C",
    "minus_two_zero": "-2 °C / 0 °C",
}
DOOR_LABELS = {"closed": "Fermée", "open": "Ouverte", "moving": "En mouvement"}

PAYLOAD_ON = "ON"
PAYLOAD_OFF = "OFF"
AVAILABLE = "online"
NOT_AVAILABLE = "offline"

APP_VERSION = "1.0"


def slugify(value: str) -> str:
    """Réduit une chaîne à un identifiant utilisable dans un topic MQTT."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "device"


def enum_value(value: Any) -> str | None:
    """Retourne la valeur brute d'un enum de la bibliothèque, ou None."""
    if value is None:
        return None
    return getattr(value, "value", value)


@dataclass
class Entity:
    """Une entité MQTT Discovery : un état publié, et parfois une commande."""

    key: str
    component: str
    name: str
    config: dict[str, Any]
    render: Callable[[Any], str | None]
    command: Callable[[str], Awaitable[None]] | None = None
    # Recopie optimiste : ce que devient le contrôle local après une commande
    # réussie, en attendant que le SSE confirme.
    optimistic: Callable[[Any, str], Any] | None = None
    has_state: bool = True
    # Contrôle HomeAPI dont l'entité dérive : (nom, zone). None pour les
    # entités purement locales comme le témoin de liaison.
    control_key: tuple[str, Any] | None = None


@dataclass
class DeviceBridge:
    """Tout ce qui concerne un appareil : son état courant et ses entités."""

    device: Device
    slug: str
    base_topic: str
    availability_topic: str
    entities: dict[str, Entity] = field(default_factory=dict)
    # Entités concernées par un contrôle donné, indexées par (nom, zone).
    by_control: dict[tuple[str, Any], list[Entity]] = field(default_factory=dict)
    # Dernier état connu de chaque contrôle, pour republier après reconnexion.
    controls: dict[tuple[str, Any], Any] = field(default_factory=dict)
    online: bool = False

    def state_topic(self, entity: Entity) -> str:
        return f"{self.base_topic}/{entity.key}/state"

    def command_topic(self, entity: Entity) -> str:
        return f"{self.base_topic}/{entity.key}/set"


class Bridge:
    """Le pont : une connexion MQTT, un flux SSE par appareil."""

    def __init__(self, config: configparser.ConfigParser) -> None:
        self._config = config
        self._api_key = config.get("liebherr", "api_key").strip()
        self._discovery_prefix = config.get(
            "mqtt", "discovery_prefix", fallback="homeassistant"
        )
        self._topic_prefix = config.get("mqtt", "topic_prefix", fallback="liebherr")
        self._refresh_interval = config.getint(
            "liebherr", "refresh_interval", fallback=900
        )
        self._client: LiebherrClient | None = None
        self._mqtt: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._devices: dict[str, DeviceBridge] = {}
        self._commands: dict[str, tuple[DeviceBridge, Entity]] = {}
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------
    # Construction des entités à partir des contrôles déclarés par l'appareil
    # ------------------------------------------------------------------

    def _zone_suffix(self, control: Any, multizone: bool) -> str:
        """Suffixe « (haut) » etc., seulement si l'appareil a plusieurs zones."""
        if not multizone:
            return ""
        position = enum_value(getattr(control, "zone_position", None))
        label = ZONE_LABELS.get(position or "", position)
        if label:
            return f" ({label})"
        zone_id = getattr(control, "zone_id", None)
        return f" (zone {zone_id})" if zone_id is not None else ""

    def _zone_key(self, control: Any) -> str:
        zone_id = getattr(control, "zone_id", None)
        return "" if zone_id is None else f"_z{zone_id}"

    def _label(self, control: Any) -> str:
        return CONTROL_LABELS.get(control.name, control.name.capitalize())

    def _build_entities(
        self, dev: DeviceBridge, control: Any, multizone: bool
    ) -> list[Entity]:
        """Traduit un contrôle de la HomeAPI en une ou plusieurs entités MQTT."""
        device_id = dev.device.device_id
        client = self._client
        assert client is not None
        suffix = self._zone_suffix(control, multizone)
        zkey = self._zone_key(control)
        label = self._label(control)
        zone_id = getattr(control, "zone_id", None)

        if isinstance(control, TemperatureControl):
            unit = (
                TemperatureUnit.FAHRENHEIT
                if enum_value(control.unit) == "°F"
                else TemperatureUnit.CELSIUS
            )
            entities = [
                Entity(
                    key=f"temperature{zkey}",
                    component="sensor",
                    name=f"Température{suffix}",
                    config={
                        "device_class": "temperature",
                        "unit_of_measurement": unit.value,
                        "state_class": "measurement",
                    },
                    render=lambda c: None if c.value is None else str(c.value),
                )
            ]

            # Certains appareils n'acceptent qu'une liste de paliers : dans ce
            # cas une liste déroulante est plus juste qu'un curseur libre.
            if control.set_temperature_steps_enabled and control.set_temperature_steps:
                steps = [str(s) for s in control.set_temperature_steps]

                async def set_step(payload: str, zid: int = zone_id, u=unit) -> None:
                    await client.set_temperature(device_id, zid, int(payload), u)

                entities.append(
                    Entity(
                        key=f"consigne{zkey}",
                        component="select",
                        name=f"Consigne{suffix}",
                        config={"options": steps},
                        render=lambda c: None if c.target is None else str(c.target),
                        command=set_step,
                        optimistic=lambda c, p: replace(c, target=int(p)),
                    )
                )
            else:

                async def set_target(payload: str, zid: int = zone_id, u=unit) -> None:
                    await client.set_temperature(
                        device_id, zid, int(round(float(payload))), u
                    )

                entities.append(
                    Entity(
                        key=f"consigne{zkey}",
                        component="number",
                        name=f"Consigne{suffix}",
                        config={
                            "min": control.min if control.min is not None else -30,
                            "max": control.max if control.max is not None else 20,
                            "step": 1,
                            "unit_of_measurement": unit.value,
                            "mode": "slider",
                            # Pas de device_class ici : le plugin Jeedom
                            # renomme la commande d'après ce champ, ce qui
                            # écraserait « Consigne » par « Température ».
                        },
                        render=lambda c: None if c.target is None else str(c.target),
                        command=set_target,
                    )
                )
            return entities

        if isinstance(control, ToggleControl):
            name = control.name

            async def set_toggle(payload: str, n: str = name, zid=zone_id) -> None:
                value = payload.strip().upper() == PAYLOAD_ON
                if n == "supercool":
                    await client.set_super_cool(device_id, zid or 0, value)
                elif n == "superfrost":
                    await client.set_super_frost(device_id, zid or 0, value)
                elif n == "partymode":
                    await client.set_party_mode(device_id, value)
                elif n == "nightmode":
                    await client.set_night_mode(device_id, value)
                else:
                    # Contrôle bascule non nommé par la bibliothèque : on parle
                    # à l'API directement, avec ou sans zone selon le contrôle.
                    body: dict[str, Any] = {"value": value}
                    if zid is not None:
                        body["zoneId"] = zid
                    await client._request(  # noqa: SLF001
                        "POST", f"devices/{device_id}/controls/{n}", json_data=body
                    )

            return [
                Entity(
                    key=f"{slugify(name)}{zkey}",
                    component="switch",
                    name=f"{label}{suffix}",
                    config={
                        "payload_on": PAYLOAD_ON,
                        "payload_off": PAYLOAD_OFF,
                        "state_on": PAYLOAD_ON,
                        "state_off": PAYLOAD_OFF,
                    },
                    render=lambda c: (
                        None
                        if c.value is None
                        else (PAYLOAD_ON if c.value else PAYLOAD_OFF)
                    ),
                    command=set_toggle,
                    optimistic=lambda c, p: replace(
                        c, value=p.strip().upper() == PAYLOAD_ON
                    ),
                )
            ]

        if isinstance(control, IceMakerControl):
            options = ["off", "on"] + (["max_ice"] if control.has_max_ice else [])
            labels = {ICE_MAKER_LABELS[o]: o for o in options}

            async def set_ice(payload: str, zid: int = zone_id) -> None:
                await client.set_ice_maker(
                    device_id, zid, IceMakerMode(labels.get(payload, payload))
                )

            return [
                Entity(
                    key=f"icemaker{zkey}",
                    component="select",
                    name=f"{label}{suffix}",
                    config={"options": [ICE_MAKER_LABELS[o] for o in options]},
                    render=lambda c: ICE_MAKER_LABELS.get(
                        enum_value(c.ice_maker_mode) or "", enum_value(c.ice_maker_mode)
                    ),
                    command=set_ice,
                )
            ]

        if isinstance(control, HydroBreezeControl):
            labels = {v: k for k, v in HYDRO_BREEZE_LABELS.items()}

            async def set_hydro(payload: str, zid: int = zone_id) -> None:
                await client.set_hydro_breeze(
                    device_id, zid, HydroBreezeMode(labels.get(payload, payload))
                )

            return [
                Entity(
                    key=f"hydrobreeze{zkey}",
                    component="select",
                    name=f"{label}{suffix}",
                    config={"options": list(HYDRO_BREEZE_LABELS.values())},
                    render=lambda c: HYDRO_BREEZE_LABELS.get(
                        enum_value(c.current_mode) or "", enum_value(c.current_mode)
                    ),
                    command=set_hydro,
                )
            ]

        if isinstance(control, BioFreshPlusControl):
            supported = [enum_value(m) for m in control.supported_modes] or list(
                BIOFRESH_PLUS_LABELS
            )
            labels = {BIOFRESH_PLUS_LABELS.get(m, m): m for m in supported}

            async def set_biofresh(payload: str, zid: int = zone_id) -> None:
                await client.set_bio_fresh_plus(
                    device_id, zid, BioFreshPlusMode(labels.get(payload, payload))
                )

            return [
                Entity(
                    key=f"biofreshplus{zkey}",
                    component="select",
                    name=f"{label}{suffix}",
                    config={"options": list(labels)},
                    render=lambda c: BIOFRESH_PLUS_LABELS.get(
                        enum_value(c.current_mode) or "", enum_value(c.current_mode)
                    ),
                    command=set_biofresh,
                )
            ]

        if isinstance(control, PresentationLightControl):
            maximum = control.max or 5

            async def set_light(payload: str) -> None:
                await client.set_presentation_light(
                    device_id, int(round(float(payload)))
                )

            return [
                Entity(
                    key="presentationlight",
                    component="number",
                    name=label,
                    config={
                        "min": 0,
                        "max": maximum,
                        "step": 1,
                        "mode": "slider",
                        "icon": "mdi:lightbulb",
                    },
                    render=lambda c: None if c.value is None else str(c.value),
                    command=set_light,
                )
            ]

        if isinstance(control, AutoDoorControl):

            async def set_door(payload: str, zid: int = zone_id) -> None:
                await client.trigger_auto_door(
                    device_id, zid, payload.strip().upper() == PAYLOAD_ON
                )

            return [
                Entity(
                    key=f"autodoor{zkey}",
                    component="sensor",
                    name=f"{label}{suffix}",
                    config={"icon": "mdi:door"},
                    render=lambda c: DOOR_LABELS.get(
                        enum_value(c.value) or "", enum_value(c.value)
                    ),
                ),
                Entity(
                    key=f"autodoor_cmd{zkey}",
                    component="switch",
                    name=f"Ouvrir la porte{suffix}",
                    config={
                        "payload_on": PAYLOAD_ON,
                        "payload_off": PAYLOAD_OFF,
                        "state_on": PAYLOAD_ON,
                        "state_off": PAYLOAD_OFF,
                        "icon": "mdi:door-open",
                    },
                    render=lambda c: (
                        PAYLOAD_ON if enum_value(c.value) == "open" else PAYLOAD_OFF
                    ),
                    command=set_door,
                ),
            ]

        LOGGER.warning(
            "Contrôle non pris en charge, ignoré : %s (%s)",
            control.name,
            type(control).__name__,
        )
        return []

    # ------------------------------------------------------------------
    # Découverte MQTT
    # ------------------------------------------------------------------

    def _discovery_topic(self, dev: DeviceBridge, entity: Entity) -> str:
        return (
            f"{self._discovery_prefix}/{entity.component}"
            f"/liebherr_{dev.slug}/{entity.key}/config"
        )

    def _device_block(self, dev: DeviceBridge) -> dict[str, Any]:
        block: dict[str, Any] = {
            "identifiers": [f"liebherr_{dev.slug}"],
            "name": dev.device.nickname or dev.device.device_name or "Liebherr",
            "manufacturer": "Liebherr",
        }
        if dev.device.device_name:
            block["model"] = dev.device.device_name
        return block

    def _publish_discovery(self, dev: DeviceBridge) -> None:
        """(Re)publie les messages de découverte de toutes les entités."""
        for entity in dev.entities.values():
            payload: dict[str, Any] = {
                "name": entity.name,
                "unique_id": f"liebherr_{dev.slug}_{entity.key}",
                "object_id": f"liebherr_{dev.slug}_{entity.key}",
                "device": self._device_block(dev),
                **entity.config,
            }
            if entity.has_state:
                payload["state_topic"] = dev.state_topic(entity)
            if entity.command is not None:
                payload["command_topic"] = dev.command_topic(entity)
            # Le témoin de liaison doit rester lisible quand le lien est coupé :
            # il est le seul à ne pas dépendre de la disponibilité.
            if entity.key != "link":
                payload["availability_topic"] = dev.availability_topic
                payload["payload_available"] = AVAILABLE
                payload["payload_not_available"] = NOT_AVAILABLE
            self._publish(
                self._discovery_topic(dev, entity),
                json.dumps(payload, ensure_ascii=False),
                retain=True,
            )

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if self._mqtt is None:
            return
        LOGGER.debug("MQTT -> %s = %s", topic, payload)
        self._mqtt.publish(topic, payload, qos=1, retain=retain)

    def _publish_states(self, dev: DeviceBridge) -> None:
        """Republie tous les états connus (après reconnexion notamment)."""
        for (name, zone), control in dev.controls.items():
            for entity in dev.by_control.get((name, zone), []):
                value = entity.render(control)
                if value is not None:
                    self._publish(dev.state_topic(entity), value)

    def _publish_availability(self, dev: DeviceBridge) -> None:
        self._publish(
            dev.availability_topic, AVAILABLE if dev.online else NOT_AVAILABLE
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Rejoue découverte, souscriptions et états à *chaque* connexion.

        En session non persistante le broker oublie les souscriptions à chaque
        coupure : tout doit être rejoué ici, sinon la passerelle continue de
        publier mais ne reçoit plus aucune commande.
        """
        if reason_code != 0:
            LOGGER.error("Connexion MQTT refusée : %s", reason_code)
            return
        LOGGER.info("Connecté au broker MQTT, (re)publication de la découverte")
        for dev in self._devices.values():
            self._publish_discovery(dev)
            self._publish_availability(dev)
            # Le testament a pu publier « hors ligne » sur le topic du temoin
            # pendant la coupure : il faut le reposer ici, sinon l'indicateur
            # reste bloque a OFF jusqu'a la prochaine bascule du flux SSE.
            self._publish_link(dev)
            self._publish_states(dev)
        for topic in self._commands:
            client.subscribe(topic, qos=1)
            LOGGER.debug("Souscription à %s", topic)
        LOGGER.info("%d topic(s) de commande souscrit(s)", len(self._commands))

    def _on_disconnect(self, client, userdata, *args):
        LOGGER.warning("Déconnexion du broker MQTT, reconnexion automatique")

    def _on_message(self, client, userdata, message):
        topic = message.topic
        payload = message.payload.decode("utf-8", "replace").strip()
        entry = self._commands.get(topic)
        if entry is None or self._loop is None:
            return
        dev, entity = entry
        LOGGER.info("Commande reçue : %s = %s", entity.name, payload)
        asyncio.run_coroutine_threadsafe(
            self._run_command(dev, entity, payload), self._loop
        )

    async def _run_command(self, dev: DeviceBridge, entity: Entity, payload: str):
        assert entity.command is not None
        try:
            await entity.command(payload)
        except LiebherrError as err:
            LOGGER.error("Échec de la commande %s = %s : %s", entity.name, payload, err)
            return
        except (ValueError, TypeError) as err:
            LOGGER.error("Valeur invalide pour %s : %r (%s)", entity.name, payload, err)
            return
        LOGGER.info("Commande %s = %s acceptée", entity.name, payload)
        # Retour immédiat, sans attendre la confirmation par le flux SSE.
        if entity.optimistic is not None and entity.control_key is not None:
            control = dev.controls.get(entity.control_key)
            if control is not None:
                try:
                    dev.controls[key] = entity.optimistic(control, payload)
                except (TypeError, ValueError):
                    pass
        self._publish(dev.state_topic(entity), payload)

    def _connect_mqtt(self) -> mqtt.Client:
        cfg = self._config
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.get("mqtt", "client_id", fallback="liebherr2mqtt"),
        )
        user = cfg.get("mqtt", "login", fallback="").strip()
        if user:
            client.username_pw_set(user, cfg.get("mqtt", "password", fallback=""))
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        # Testament : si la passerelle meurt, le témoin « En ligne » retombe
        # tout seul côté Jeedom. Le protocole n'autorise qu'un seul testament
        # par connexion, donc seul le premier appareil en bénéficie.
        first = next(iter(self._devices.values()), None)
        if first is not None:
            link = first.entities.get("link")
            if link is not None:
                client.will_set(
                    first.state_topic(link), PAYLOAD_OFF, qos=1, retain=True
                )
        if len(self._devices) > 1:
            LOGGER.warning(
                "%d appareils : seul %s a un testament MQTT",
                len(self._devices),
                first.device.device_id if first else "-",
            )
        client.connect_async(
            cfg.get("mqtt", "host", fallback="127.0.0.1"),
            cfg.getint("mqtt", "port", fallback=1883),
            keepalive=60,
        )
        client.loop_start()
        return client

    # ------------------------------------------------------------------
    # Flux d'états
    # ------------------------------------------------------------------

    def _merge(self, dev: DeviceBridge, controls: list[Any]) -> None:
        """Fusionne un lot de contrôles (complet ou delta) et publie."""
        for control in controls:
            key = (control.name, getattr(control, "zone_id", None))
            dev.controls[key] = control
            for entity in dev.by_control.get(key, []):
                value = entity.render(control)
                if value is not None:
                    self._publish(dev.state_topic(entity), value)

    async def _stream(self, dev: DeviceBridge) -> None:
        """Boucle SSE d'un appareil : reconnexion et backoff par la bibliothèque."""
        assert self._client is not None

        def on_up() -> None:
            dev.online = True
            self._publish_availability(dev)
            self._publish_link(dev)
            LOGGER.info("Flux temps réel établi pour %s", dev.device.device_id)

        def on_down() -> None:
            dev.online = False
            self._publish_availability(dev)
            self._publish_link(dev)
            LOGGER.warning("Flux temps réel perdu pour %s", dev.device.device_id)

        async for controls in self._client.stream_controls_forever(
            dev.device.device_id, on_connect=on_up, on_disconnect=on_down
        ):
            self._merge(dev, controls)

    def _publish_link(self, dev: DeviceBridge) -> None:
        entity = dev.entities.get("link")
        if entity is not None:
            self._publish(
                dev.state_topic(entity), PAYLOAD_ON if dev.online else PAYLOAD_OFF
            )

    async def _periodic_refresh(self) -> None:
        """Filet de sécurité : relecture complète espacée, au cas où le SSE dérive."""
        if self._refresh_interval <= 0:
            return
        assert self._client is not None
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._refresh_interval
                )
                return
            except asyncio.TimeoutError:
                pass
            for dev in self._devices.values():
                try:
                    controls = await self._client.get_controls(dev.device.device_id)
                except LiebherrError as err:
                    LOGGER.warning(
                        "Relecture périodique impossible pour %s : %s",
                        dev.device.device_id,
                        err,
                    )
                    continue
                LOGGER.debug("Relecture périodique de %s", dev.device.device_id)
                self._merge(dev, controls)

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Découvre les appareils et construit toutes les entités."""
        assert self._client is not None
        devices = await self._client.get_devices()
        if not devices:
            raise SystemExit(
                "Aucun appareil retourné par la HomeAPI : vérifier que la clé "
                "est valide et que l'appareil est bien connecté au Wi-Fi."
            )
        for device in devices:
            slug = slugify(device.device_id)
            dev = DeviceBridge(
                device=device,
                slug=slug,
                base_topic=f"{self._topic_prefix}/{slug}",
                availability_topic=f"{self._topic_prefix}/{slug}/availability",
            )
            controls = await self._client.get_controls(device.device_id)
            zones = {
                getattr(c, "zone_id", None)
                for c in controls
                if getattr(c, "zone_id", None) is not None
            }
            multizone = len(zones) > 1
            for control in controls:
                key = (control.name, getattr(control, "zone_id", None))
                dev.controls[key] = control
                entities = self._build_entities(dev, control, multizone)
                dev.by_control.setdefault(key, []).extend(entities)
                for entity in entities:
                    entity.control_key = key
                    dev.entities[entity.key] = entity

            # Témoin de liaison : visible même quand l'appareil est injoignable.
            link = Entity(
                key="link",
                component="binary_sensor",
                name="En ligne",
                config={
                    "device_class": "connectivity",
                    "payload_on": PAYLOAD_ON,
                    "payload_off": PAYLOAD_OFF,
                },
                render=lambda c: None,
            )
            dev.entities["link"] = link

            self._devices[device.device_id] = dev
            for entity in dev.entities.values():
                if entity.command is not None:
                    self._commands[dev.command_topic(entity)] = (dev, entity)
            LOGGER.info(
                "Appareil %s (%s, %s) : %d contrôle(s), %d entité(s)",
                device.nickname or device.device_name,
                device.device_id,
                enum_value(device.device_type),
                len(controls),
                len(dev.entities),
            )

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._client = LiebherrClient(self._api_key)
        try:
            await self.setup()
            self._mqtt = self._connect_mqtt()
            tasks = [
                asyncio.create_task(self._stream(dev)) for dev in self._devices.values()
            ]
            tasks.append(asyncio.create_task(self._periodic_refresh()))
            stop = asyncio.create_task(self._stopping.wait())
            done, _ = await asyncio.wait(
                [*tasks, stop], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stop and task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Arrêt propre : appareils marqués hors ligne, sessions fermées."""
        LOGGER.info("Arrêt de la passerelle")
        if self._mqtt is not None:
            for dev in self._devices.values():
                dev.online = False
                self._publish_availability(dev)
                self._publish_link(dev)
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            self._mqtt = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    def request_stop(self) -> None:
        self._stopping.set()


# Variables d'environnement reconnues, et où elles atterrissent dans la
# configuration. Elles priment sur le fichier : c'est ce qui permet de tout
# régler depuis les champs d'un gabarit Unraid, sans éditer de fichier.
ENV_OVERRIDES = {
    "LIEBHERR_API_KEY": ("liebherr", "api_key"),
    "LIEBHERR_REFRESH_INTERVAL": ("liebherr", "refresh_interval"),
    "MQTT_HOST": ("mqtt", "host"),
    "MQTT_PORT": ("mqtt", "port"),
    "MQTT_USER": ("mqtt", "login"),
    "MQTT_PASSWORD": ("mqtt", "password"),
    "MQTT_CLIENT_ID": ("mqtt", "client_id"),
    "MQTT_DISCOVERY_PREFIX": ("mqtt", "discovery_prefix"),
    "MQTT_TOPIC_PREFIX": ("mqtt", "topic_prefix"),
    "LOG_LEVEL": ("log", "level"),
}


def load_config(path: str) -> configparser.ConfigParser:
    """Charge le fichier de configuration, puis applique l'environnement.

    Le fichier est facultatif : une configuration entièrement fournie par
    variables d'environnement est valide.
    """
    config = configparser.ConfigParser()
    read = config.read(path, encoding="utf-8")

    for env_name, (section, option) in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, option, value)

    if not config.has_option("liebherr", "api_key"):
        raise SystemExit(
            "Aucune clé API. Renseigner LIEBHERR_API_KEY, ou 'api_key' dans la "
            f"section [liebherr] de {path}"
            + ("" if read else f" (fichier absent : {path})")
        )
    return config


def print_banner(config: configparser.ConfigParser) -> None:
    """Affiche une bannière de démarrage avec un récap de la configuration."""

    def g(section: str, opt: str, default: str = "") -> str:
        return config.get(section, opt, fallback=default) if config.has_section(section) else default

    lines = [
        f"Version    : {APP_VERSION}",
        f"MQTT       : {g('mqtt', 'host', '127.0.0.1')}:{g('mqtt', 'port', '1883')}"
        f"  (discovery={g('mqtt', 'discovery_prefix', 'homeassistant')}, topics={g('mqtt', 'topic_prefix', 'liebherr')})",
        f"HomeAPI    : clé api-key, états en push (SSE) — filet {g('liebherr', 'refresh_interval', '900')}s",
    ]
    art = [
        r"  _ _      _   _                    ___              _   _   ",
        r" | (_)___ | |_| |_  ___ _ _ _ _  __|_  )_ __  __ _ _| |_| |_ ",
        r" | | / -_)| '_| ' \/ -_) '_| '_|/ -_/ /| '  \/ _` |  _|  _|  ",
        r" |_|_\___||_,_|_||_\___|_| |_| \___/___|_|_|_\__, |\__|\__|  ",
        r"                Liebherr SmartDevice -> MQTT   |_|          ",
    ]
    width = max(max(len(a) for a in art), max(len(l) for l in lines)) + 2
    out = ["", "+" + "-" * width + "+"]
    for a in art:
        out.append("|" + a.ljust(width) + "|")
    out.append("+" + "-" * width + "+")
    for l in lines:
        out.append("| " + l.ljust(width - 1) + "|")
    out.append("+" + "-" * width + "+")
    out.append("")
    print("\n".join(out), flush=True)


async def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH
    config = load_config(path)
    logging.basicConfig(
        level=config.get("log", "level", fallback="INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    print_banner(config)
    bridge = Bridge(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bridge.request_stop)
    try:
        await bridge.run()
    except LiebherrAuthenticationError:
        LOGGER.error("Clé API refusée par Liebherr — la régénérer dans l'application")
        return 2
    except (LiebherrNotFoundError, LiebherrPreconditionFailedError) as err:
        LOGGER.error("Appareil injoignable ou non enrôlé : %s", err)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
