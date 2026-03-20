"""
🔧 Módulo de Normalización de Vehículos v4.0
=============================================
Normaliza marca, modelo y versión usando diccionarios enriquecidos
generados desde catálogo JSON (normalizer_dicts.json).

MEJORAS RESPECTO A v3.0:
- P0: Usa metadata enriquecida (años, motorizaciones, aliases, confusiones)
- P0: No fuzzy para modelos numéricos puros
- P0: Blacklist de motorizaciones (no confundir motor con modelo)
- P1: Extrae año del anuncio y lo usa como filtro/validación
- P1: Ponderación de fuentes (título > marca_raw > modelo_raw > etc.)
- P1: Códigos de modelo para TODAS las marcas (desde el JSON enriquecido)
- P2: Post-validación con año, sucesión y confusiones conocidas
- P2: Confidence score (0-100)
- P2: Tabla de confusiones conocidas
- P3: Normalización en dos pasadas

ESTRATEGIA DE BÚSQUEDA (en orden):
1. Match exacto en catálogo (modelo directo)
2. Match por alias de modelo
3. Mapeo de códigos (C200→Clase C, 320i→Serie 3)
4. Match de nombre base (sin números/códigos)
5. Fuzzy matching (SOLO para modelos no numéricos, restrictivo)
6. Fallback a datos originales
+ Post-validación con año y confusiones

Autor: Sistema de Normalización
Versión: 4.0
"""

import re
import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════
# 📦 DEPENDENCIAS OPCIONALES
# ═══════════════════════════════════════════════════════════════

try:
    from unidecode import unidecode
    HAS_UNIDECODE = True
except ImportError:
    HAS_UNIDECODE = False
    def unidecode(text: str) -> str:
        replacements = {
            'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
            'ä': 'a', 'ë': 'e', 'ï': 'i', 'ö': 'o', 'ü': 'u',
            'ñ': 'n', 'ç': 'c',
            'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U',
            'Ñ': 'N', 'Ü': 'U',
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 🔧 CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

@dataclass
class NormalizerConfig:
    """Configuración del normalizador v4."""
    # Fuzzy
    fuzzy_threshold_marca: int = 85
    fuzzy_threshold_modelo: int = 88
    fuzzy_threshold_version: int = 80
    fuzzy_min_word_length: int = 4
    enable_fuzzy: bool = True

    # Confianza mínima para aceptar un resultado sin flag
    min_confidence_accept: int = 60

    # Rutas
    dicts_path: str = "config/normalizer_dicts.json"
    brand_aliases_path: str = "config/brand_aliases.json"


CONFIG = NormalizerConfig()


# ═══════════════════════════════════════════════════════════════
# 📊 ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════

@dataclass
class NormalizationStats:
    total: int = 0
    full_match: int = 0
    partial_match: int = 0
    marca_only: int = 0
    fallback_used: int = 0
    year_corrected: int = 0
    confusion_detected: int = 0
    motorization_filtered: int = 0
    numeric_exact_only: int = 0

    by_method: Dict[str, int] = field(default_factory=lambda: {
        'exact': 0, 'alias': 0, 'code_mapping': 0,
        'base_name': 0, 'fuzzy': 0, 'fallback': 0
    })

    by_confidence: Dict[str, int] = field(default_factory=lambda: {
        'high': 0, 'medium': 0, 'low': 0, 'very_low': 0
    })

    def summary(self) -> str:
        if self.total == 0:
            return "Sin datos procesados"
        p = lambda x: f"{(x/self.total*100):.1f}%"
        return (
            f"Total: {self.total} | "
            f"Full: {self.full_match} ({p(self.full_match)}) | "
            f"Parcial: {self.partial_match} ({p(self.partial_match)}) | "
            f"Fallback: {self.fallback_used} ({p(self.fallback_used)}) | "
            f"Año corregido: {self.year_corrected} | "
            f"Confusión detectada: {self.confusion_detected}"
        )


STATS = NormalizationStats()


# ═══════════════════════════════════════════════════════════════
# 🏷️ ALIASES DE MARCAS (hardcoded + archivo)
# ═══════════════════════════════════════════════════════════════

DEFAULT_BRAND_ALIASES = {
    'vw': 'volkswagen', 'volks': 'volkswagen', 'volkswagon': 'volkswagen',
    'mercedes': 'mercedes-benz', 'mercedes benz': 'mercedes-benz',
    'mercedesbenz': 'mercedes-benz', 'mb': 'mercedes-benz', 'benz': 'mercedes-benz',
    'chevy': 'chevrolet', 'gm': 'chevrolet',
    'alfa': 'alfa romeo',
    'landrover': 'land rover',
    'citroen': 'citroën', 'citröen': 'citroën',
}


# ═══════════════════════════════════════════════════════════════
# 🚫 BLACKLIST PARA FUZZY
# ═══════════════════════════════════════════════════════════════

FUZZY_BLACKLIST = {
    'ano', 'año', 'años', 'anio',
    'nafta', 'naftero', 'diesel', 'gasoil', 'gnc', 'gas', 'hibrido', 'electrico',
    'manual', 'automatico', 'automatica', 'secuencial', 'cvt', 'dsg',
    'muy', 'buen', 'bueno', 'buena', 'mal', 'estado', 'impecable',
    'excelente', 'perfecto', 'perfecta', 'inmaculado', 'nuevo', 'nueva', 'usado',
    'unico', 'unica', 'dueno', 'titular', 'particular',
    'full', 'tope', 'gama', 'equipo', 'equipado', 'completo', 'cuero', 'techo',
    'permuto', 'permuta', 'financio', 'vendo', 'venta', 'contado',
    'auto', 'autos', 'coche', 'vehiculo', 'carro', 'motor', 'caja',
    'vtv', 'papeles', 'patente', 'seguro', 'service',
    'consultar', 'precio', 'oferta', 'urgente', 'oportunidad',
    'original', 'funcionando', 'kilometros', 'kms', 'litros',
    'puertas', 'modelo', 'version', 'serie', 'linea', 'tipo',
}


# ═══════════════════════════════════════════════════════════════
# 🛠️ FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════

def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = str(text).lower().strip()
    text = unidecode(text)
    text = re.sub(r'[^\w\s\-]', ' ', text)
    text = ' '.join(text.split())
    return text


def normalize_key(text: str) -> str:
    if not text:
        return ""
    return normalize_text(text)


def make_key(marca: str, modelo: str) -> str:
    return f"{normalize_key(marca)}|{normalize_key(modelo)}"


def find_exact_in_text(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    pattern = r'\b' + re.escape(needle) + r'\b'
    return bool(re.search(pattern, haystack, re.IGNORECASE))


def is_numeric_model(modelo: str) -> bool:
    """Verifica si un modelo es puramente numérico o alfanumérico corto."""
    m = normalize_key(modelo)
    return bool(re.match(r'^\d{2,5}$', m) or re.match(r'^[a-z]\d{1,3}$', m))


def extract_year_from_text(text: str) -> Optional[int]:
    """Extrae el año más probable de un texto."""
    if not text:
        return None
    matches = re.findall(r'\b(19[5-9]\d|20[0-2]\d)\b', text)
    if not matches:
        return None
    # Preferir años más recientes (más probables como año del auto)
    years = [int(y) for y in matches]
    # Filtrar años futuros
    years = [y for y in years if y <= 2026]
    return max(years) if years else None


def is_motorization(text: str, blacklist: Dict) -> bool:
    """Verifica si un texto es una motorización conocida (no un modelo)."""
    text_norm = normalize_key(text)
    if not text_norm:
        return False

    # Verificar en blacklist exacta
    exact_patterns = blacklist.get('exact', [])
    if text_norm in exact_patterns:
        return True

    # Verificar patrones regex
    for pattern in blacklist.get('regex', []):
        if re.match(pattern, text_norm):
            return True

    return False


def fuzzy_match(needle: str, candidates: List[str], threshold: int = 85) -> Optional[Tuple[str, int]]:
    if not HAS_RAPIDFUZZ or not needle or not candidates:
        return None
    if len(needle) < CONFIG.fuzzy_min_word_length:
        return None
    if needle.lower() in FUZZY_BLACKLIST:
        return None
    try:
        result = process.extractOne(needle, candidates, scorer=fuzz.ratio, score_cutoff=threshold)
        return (result[0], result[1]) if result else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 📚 CATÁLOGO ENRIQUECIDO (desde normalizer_dicts.json)
# ═══════════════════════════════════════════════════════════════

class EnrichedCatalog:
    """
    Catálogo cargado desde normalizer_dicts.json.
    Contiene toda la metadata enriquecida.
    """

    def __init__(self):
        self._data: Dict = {}
        self._brand_aliases: Dict[str, str] = {}
        self._loaded = False

    def load(self, dicts_path: str, brand_aliases_path: str = None) -> bool:
        if not os.path.exists(dicts_path):
            logger.warning(f"⚠️ Archivo no encontrado: {dicts_path}")
            return False
        try:
            with open(dicts_path, 'r', encoding='utf-8') as f:
                self._data = json.load(f)
            self._loaded = True

            # Cargar brand aliases
            for alias, brand in DEFAULT_BRAND_ALIASES.items():
                self._brand_aliases[normalize_key(alias)] = normalize_key(brand)

            if brand_aliases_path and os.path.exists(brand_aliases_path):
                with open(brand_aliases_path, 'r', encoding='utf-8') as f:
                    extra = json.load(f)
                for alias, brand in extra.get('brand_aliases', {}).items():
                    self._brand_aliases[normalize_key(alias)] = normalize_key(brand)

            meta = self._data.get('_metadata', {})
            logger.info(
                f"✅ Catálogo cargado: {meta.get('total_brands', 0)} marcas, "
                f"{meta.get('total_models', 0)} modelos, "
                f"{meta.get('total_versions', 0)} versiones"
            )
            return True
        except Exception as e:
            logger.error(f"❌ Error cargando catálogo: {e}")
            return False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # ── Acceso a diccionarios ──

    @property
    def catalog_index(self) -> Dict:
        return self._data.get('catalog_index', {})

    @property
    def code_mappings(self) -> Dict:
        return self._data.get('model_code_to_catalog', {})

    @property
    def model_aliases(self) -> Dict:
        return self._data.get('model_aliases', {})

    @property
    def year_ranges(self) -> Dict:
        return self._data.get('year_ranges', {})

    @property
    def known_confusions(self) -> Dict:
        return self._data.get('known_confusions', {})

    @property
    def succession_map(self) -> Dict:
        return self._data.get('succession_map', {})

    @property
    def known_motorizations(self) -> Dict:
        return self._data.get('known_motorizations', {})

    @property
    def motorization_blacklist(self) -> Dict:
        return self._data.get('motorization_blacklist', {})

    @property
    def valid_versions(self) -> Dict:
        return self._data.get('valid_versions', {})

    @property
    def validation_rules(self) -> Dict:
        return self._data.get('validation_rules', {})

    @property
    def vehicle_classification(self) -> Dict:
        return self._data.get('vehicle_classification', {})

    @property
    def model_parent_map(self) -> Dict:
        return self._data.get('model_parent_map', {})

    # ── Métodos de consulta ──

    def resolve_brand(self, brand_raw: str) -> str:
        b = normalize_key(brand_raw)
        if b in self._brand_aliases:
            return self._brand_aliases[b]
        compact = b.replace(' ', '').replace('-', '')
        if compact in self._brand_aliases:
            return self._brand_aliases[compact]
        return b

    def has_brand(self, brand: str) -> bool:
        return normalize_key(brand) in self.catalog_index

    def get_brands(self) -> List[str]:
        return sorted(self.catalog_index.keys(), key=len, reverse=True)

    def get_models(self, brand: str) -> List[str]:
        b = normalize_key(brand)
        models = self.catalog_index.get(b, {})
        return sorted(models.keys(), key=len, reverse=True)

    def has_model(self, brand: str, model: str) -> bool:
        b = normalize_key(brand)
        m = normalize_key(model)
        return m in self.catalog_index.get(b, {})

    def get_versions(self, brand: str, model: str) -> List[str]:
        key = make_key(brand, model)
        return self.valid_versions.get(key, [])

    def get_year_range(self, brand: str, model: str) -> Optional[Dict]:
        key = make_key(brand, model)
        return self.year_ranges.get(key)

    def is_exact_match_only(self, brand: str, model: str) -> bool:
        key = make_key(brand, model)
        return key in self.validation_rules.get('exact_match_only', [])

    def get_year_suggestion(self, brand: str, model: str, year: int) -> Optional[str]:
        """Si el año no coincide, sugiere sucesor o predecesor."""
        key = make_key(brand, model)
        suggestion = self.validation_rules.get('year_mismatch_suggestions', {}).get(key)
        if not suggestion:
            return None
        rango = suggestion.get('rango_valido', {})
        if year > rango.get('hasta', 9999):
            return suggestion.get('si_año_posterior')
        elif year < rango.get('desde', 0):
            return suggestion.get('si_año_anterior')
        return None

    def resolve_model_alias(self, brand: str, alias: str) -> Optional[str]:
        b = normalize_key(brand)
        a = normalize_key(alias)
        return self.model_aliases.get(b, {}).get(a)

    def resolve_code(self, brand: str, code: str) -> Optional[Any]:
        b = normalize_key(brand)
        c = normalize_key(code)
        return self.code_mappings.get(b, {}).get(c)

    def get_confusions(self, brand: str, model: str) -> List[Dict]:
        key = make_key(brand, model)
        return self.known_confusions.get(key, [])

    def get_successor(self, brand: str, model: str) -> Optional[str]:
        b = normalize_key(brand)
        m = normalize_key(model)
        return self.succession_map.get(b, {}).get(m, {}).get('sucesor')

    def get_predecessor(self, brand: str, model: str) -> Optional[str]:
        b = normalize_key(brand)
        m = normalize_key(model)
        return self.succession_map.get(b, {}).get(m, {}).get('predecesor')


# Instancia global
CATALOG = EnrichedCatalog()


# ═══════════════════════════════════════════════════════════════
# 🎯 NORMALIZADOR v4.0
# ═══════════════════════════════════════════════════════════════

class VehicleNormalizerV4:
    """
    Normalizador v4.0 con metadata enriquecida.

    Pipeline:
    PASADA 1 (rápida):
      1. Extraer año del anuncio
      2. Encontrar marca
      3. Filtrar motorizaciones del input
      4. Encontrar modelo (exacto → alias → código → base_name → fuzzy)
      5. Encontrar versión

    PASADA 2 (validación):
      6. Validar año vs modelo (¿existía ese modelo ese año?)
      7. Verificar confusiones conocidas
      8. Calcular confidence score
      9. Aplicar correcciones si es necesario
    """

    def __init__(self, catalog: EnrichedCatalog = None):
        self.catalog = catalog or CATALOG

    def normalize(
        self,
        titulo: str = "",
        descripcion: str = "",
        marca_raw: str = "",
        modelo_raw: str = "",
        version_raw: str = "",
        año_raw: Optional[int] = None,
        precio_raw: Optional[float] = None,
    ) -> Dict:
        global STATS
        STATS.total += 1

        # ── Normalizar inputs ──
        titulo_n = normalize_text(titulo)
        desc_n = normalize_text(descripcion[:500] if descripcion else "")
        marca_n = normalize_text(marca_raw)
        modelo_n = normalize_text(modelo_raw)
        version_n = normalize_text(version_raw)

        # Textos combinados con ponderación
        # (título y marca_raw son más confiables)
        text_high = f"{titulo_n} {marca_n}"
        text_medium = f"{version_n} {modelo_n}"
        text_low = desc_n
        text_all = f"{text_high} {text_medium} {text_low}"

        result = {
            'marca': None,
            'modelo': None,
            'version': None,
            'año_detectado': None,
            'norm_status': 'pending',
            'from_catalog': False,
            'match_method': None,
            'confidence': 0,
            'confidence_level': 'very_low',
            'warnings': [],
            'corrections': [],
        }

        # ═════════════════════════════════════════════
        # PASO 0: EXTRAER AÑO
        # ═════════════════════════════════════════════
        año = año_raw
        if not año:
            año = extract_year_from_text(titulo_n)
        if not año:
            año = extract_year_from_text(text_all)
        result['año_detectado'] = año

        # ═════════════════════════════════════════════
        # PASO 1: ENCONTRAR MARCA
        # ═════════════════════════════════════════════
        marca_result = self._find_marca(marca_n, text_all)
        if not marca_result:
            STATS.fallback_used += 1
            result['norm_status'] = 'no_marca'
            result['confidence'] = 5
            result['confidence_level'] = 'very_low'
            return result

        result['marca'] = marca_result['value']
        result['from_catalog'] = marca_result['from_catalog']
        marca = result['marca']

        # ═════════════════════════════════════════════
        # PASO 2: FILTRAR MOTORIZACIONES DEL INPUT
        # ═════════════════════════════════════════════
        modelo_candidates = [modelo_n, version_n]
        filtered_candidates = []
        for candidate in modelo_candidates:
            if candidate and not self._is_motorization(candidate, marca):
                filtered_candidates.append(candidate)
            elif candidate:
                STATS.motorization_filtered += 1
                result['warnings'].append(
                    f"'{candidate}' filtrado como motorización, no modelo"
                )

        # ═════════════════════════════════════════════
        # PASO 3: ENCONTRAR MODELO (Pasada 1)
        # ═════════════════════════════════════════════
        modelo_result = self._find_modelo(
            marca=marca,
            titulo=titulo_n,
            modelo_raw=modelo_n,
            version_raw=version_n,
            text_high=text_high,
            text_medium=text_medium,
            text_all=text_all,
        )

        if modelo_result:
            result['modelo'] = modelo_result['value']
            result['match_method'] = modelo_result['method']
            if modelo_result['from_catalog']:
                result['from_catalog'] = True
            STATS.by_method[modelo_result['method']] = \
                STATS.by_method.get(modelo_result['method'], 0) + 1

        # ═════════════════════════════════════════════
        # PASO 4: ENCONTRAR VERSIÓN
        # ═════════════════════════════════════════════
        if result['from_catalog'] and result['modelo']:
            version_result = self._find_version(
                marca=marca,
                modelo=result['modelo'],
                search_text=text_all,
            )
            if version_result:
                result['version'] = version_result['value']

        # ═════════════════════════════════════════════
        # PASO 5: POST-VALIDACIÓN (Pasada 2)
        # ═════════════════════════════════════════════
        if result['from_catalog'] and result['modelo']:
            self._post_validate(result, marca, año)

        # ═════════════════════════════════════════════
        # PASO 6: CALCULAR CONFIDENCE
        # ═════════════════════════════════════════════
        self._calculate_confidence(result)

        # ═════════════════════════════════════════════
        # PASO 7: STATUS FINAL
        # ═════════════════════════════════════════════
        self._set_final_status(result)

        return result

    # ─────────────────────────────────────────────
    # MARCA
    # ─────────────────────────────────────────────

    def _find_marca(self, marca_raw: str, text: str) -> Optional[Dict]:
        if not self.catalog.is_loaded:
            if marca_raw:
                return {'value': marca_raw, 'from_catalog': False, 'method': 'fallback'}
            return None

        # 1. Resolver alias y verificar
        if marca_raw:
            resolved = self.catalog.resolve_brand(marca_raw)
            if self.catalog.has_brand(resolved):
                return {'value': resolved, 'from_catalog': True, 'method': 'exact'}

        # 2. Buscar en texto
        for brand in self.catalog.get_brands():
            if find_exact_in_text(brand, text):
                return {'value': brand, 'from_catalog': True, 'method': 'text_search'}

        # 3. Fuzzy
        if CONFIG.enable_fuzzy and marca_raw:
            match = fuzzy_match(marca_raw, self.catalog.get_brands(), CONFIG.fuzzy_threshold_marca)
            if match:
                return {'value': match[0], 'from_catalog': True, 'method': 'fuzzy'}

        # 4. Fallback
        if marca_raw:
            return {'value': marca_raw, 'from_catalog': False, 'method': 'fallback'}
        return None

    # ─────────────────────────────────────────────
    # MOTORIZACIÓN FILTER
    # ─────────────────────────────────────────────

    def _is_motorization(self, text: str, marca: str) -> bool:
        """Verifica si el texto es una motorización conocida."""
        text_norm = normalize_key(text)

        # Verificar en blacklist global
        if is_motorization(text_norm, self.catalog.motorization_blacklist):
            return True

        # Verificar en motorizaciones conocidas de la marca
        marca_motors = self.catalog.known_motorizations.get(normalize_key(marca), {})
        if text_norm in marca_motors:
            return True

        # Patrones regex genéricos de motorización
        motor_patterns = [
            r'^\d+\.\d+\s*(tdi|tfsi|hdi|vti|thp|jtd|cdti|dci|tsi|fsi|mpi|ts|jts)$',
            r'^\d+\.\d+\s*(turbo|diesel|nafta|naftero)$',
            r'^\d+\.\d+\s*l$',
            r'^\d+(v|cv|hp)$',
            r'^(v6|v8|v10|v12|i4|i6)$',
        ]
        for pattern in motor_patterns:
            if re.match(pattern, text_norm):
                return True

        return False

    # ─────────────────────────────────────────────
    # MODELO
    # ─────────────────────────────────────────────

    def _find_modelo(
        self, marca: str, titulo: str, modelo_raw: str,
        version_raw: str, text_high: str, text_medium: str, text_all: str
    ) -> Optional[Dict]:

        marca_norm = normalize_key(marca)
        modelos_catalogo = self.catalog.get_models(marca)

        if not modelos_catalogo:
            # Marca no está en catálogo, fallback
            return self._fallback_modelo(modelo_raw, version_raw, titulo, marca)

        combined = f"{version_raw} {modelo_raw} {titulo} {text_all}"

        # ── ESTRATEGIA 1: Match exacto ──
        result = self._try_exact_match(modelos_catalogo, combined)
        if result:
            return result

        # ── ESTRATEGIA 2: Match por alias de modelo ──
        result = self._try_alias_match(marca_norm, combined)
        if result:
            return result

        # ── ESTRATEGIA 3: Mapeo de códigos ──
        result = self._try_code_mapping(marca_norm, marca, modelos_catalogo, combined)
        if result:
            return result

        # ── ESTRATEGIA 4: Match de nombre base ──
        result = self._try_base_name_match(
            modelos_catalogo, [version_raw, modelo_raw, titulo]
        )
        if result:
            return result

        # ── ESTRATEGIA 5: Fuzzy (solo modelos no numéricos) ──
        result = self._try_fuzzy_match(marca, modelos_catalogo, combined)
        if result:
            return result

        # ── ESTRATEGIA 6: Fallback ──
        return self._fallback_modelo(modelo_raw, version_raw, titulo, marca)

    def _try_exact_match(self, modelos: List[str], text: str) -> Optional[Dict]:
        """Busca match exacto de modelo en el texto."""
        for modelo in modelos:
            if find_exact_in_text(modelo, text):
                return {
                    'value': modelo, 'from_catalog': True, 'method': 'exact'
                }
        return None

    def _try_alias_match(self, marca_norm: str, text: str) -> Optional[Dict]:
        """Busca modelo usando aliases conocidos."""
        aliases = self.catalog.model_aliases.get(marca_norm, {})
        for alias, modelo_real in aliases.items():
            if find_exact_in_text(alias, text):
                # Verificar que el modelo real existe
                if self.catalog.has_model(marca_norm, modelo_real):
                    return {
                        'value': modelo_real, 'from_catalog': True, 'method': 'alias'
                    }
        return None

    def _try_code_mapping(
        self, marca_norm: str, marca: str,
        modelos: List[str], text: str
    ) -> Optional[Dict]:
        """Mapea códigos (C200→Clase C, 320i→Serie 3)."""
        codes = self.catalog.code_mappings.get(marca_norm, {})
        if not codes:
            return None

        # Extraer posibles códigos del texto
        text_words = normalize_key(text).split()

        # Buscar de más largo a más corto para evitar matches parciales
        sorted_codes = sorted(codes.keys(), key=len, reverse=True)

        for code in sorted_codes:
            if find_exact_in_text(code, text):
                mapped = codes[code]

                # Si es ambiguo, tomar el primero (se podría mejorar)
                if isinstance(mapped, dict):
                    if mapped.get('ambiguo'):
                        modelo_real = mapped['modelos'][0]
                    else:
                        continue
                else:
                    modelo_real = mapped

                if self.catalog.has_model(marca, modelo_real):
                    return {
                        'value': modelo_real,
                        'from_catalog': True,
                        'method': 'code_mapping'
                    }
        return None

    def _try_base_name_match(
        self, modelos: List[str], text_sources: List[str]
    ) -> Optional[Dict]:
        """Busca modelo por nombre base (sin números)."""
        words_to_check = []
        for text in text_sources:
            if text:
                words = [
                    w for w in text.split()
                    if len(w) >= 3 and not re.match(r'^\d+$', w)
                ]
                words_to_check.extend(words)

        for word in words_to_check:
            word_clean = re.sub(r'\d+', '', word).strip()
            if word_clean and len(word_clean) >= 3:
                for modelo in modelos:
                    if word_clean == modelo or find_exact_in_text(word_clean, modelo):
                        return {
                            'value': modelo,
                            'from_catalog': True,
                            'method': 'base_name'
                        }
        return None

    def _try_fuzzy_match(
        self, marca: str, modelos: List[str], text: str
    ) -> Optional[Dict]:
        """Fuzzy matching RESTRINGIDO: NO para modelos numéricos."""
        if not CONFIG.enable_fuzzy or not modelos:
            return None

        # Filtrar modelos numéricos del fuzzy
        fuzzy_candidates = [
            m for m in modelos if not is_numeric_model(m)
        ]

        if not fuzzy_candidates:
            STATS.numeric_exact_only += 1
            return None

        valid_words = [
            w for w in text.split()
            if (len(w) >= CONFIG.fuzzy_min_word_length
                and w.lower() not in FUZZY_BLACKLIST
                and not re.match(r'^(19|20)\d{2}$', w)
                and not re.match(r'^\d+$', w))
        ]

        for word in valid_words:
            match = fuzzy_match(word, fuzzy_candidates, CONFIG.fuzzy_threshold_modelo)
            if match:
                return {
                    'value': match[0],
                    'from_catalog': True,
                    'method': 'fuzzy'
                }
        return None

    def _fallback_modelo(
        self, modelo_raw: str, version_raw: str,
        titulo: str, marca: str
    ) -> Optional[Dict]:
        """Extrae modelo como fallback."""
        combined = f"{version_raw} {modelo_raw} {titulo}"
        if marca:
            combined = re.sub(
                r'\b' + re.escape(marca) + r'\b', '', combined, flags=re.IGNORECASE
            )
            combined = re.sub(
                r'\b' + re.escape(marca.replace('-', ' ')) + r'\b',
                '', combined, flags=re.IGNORECASE
            )

        for word in combined.split():
            w = word.strip()
            if (len(w) >= 2
                and not re.match(r'^(19|20)\d{2}$', w)
                and w.lower() not in FUZZY_BLACKLIST):
                return {'value': w, 'from_catalog': False, 'method': 'fallback'}
        return None

    # ─────────────────────────────────────────────
    # VERSIÓN
    # ─────────────────────────────────────────────

    def _find_version(self, marca: str, modelo: str, search_text: str) -> Optional[Dict]:
        versiones = self.catalog.get_versions(marca, modelo)
        if not versiones:
            return None

        # Ordenar por longitud descendente para match más específico primero
        versiones_sorted = sorted(versiones, key=len, reverse=True)

        for version in versiones_sorted:
            if find_exact_in_text(version, search_text):
                return {'value': version, 'from_catalog': True, 'method': 'exact'}

        if CONFIG.enable_fuzzy:
            valid_words = [
                w for w in search_text.split()
                if len(w) >= 3 and w.lower() not in FUZZY_BLACKLIST
            ]
            for word in valid_words:
                match = fuzzy_match(word, versiones, CONFIG.fuzzy_threshold_version)
                if match:
                    return {'value': match[0], 'from_catalog': True, 'method': 'fuzzy'}

        return None

    # ─────────────────────────────────────────────
    # POST-VALIDACIÓN (Pasada 2)
    # ─────────────────────────────────────────────

    def _post_validate(self, result: Dict, marca: str, año: Optional[int]):
        """
        Validaciones posteriores:
        1. ¿El modelo existía en el año detectado?
        2. ¿Hay confusiones conocidas que debamos verificar?
        3. ¿El modelo es variante de otro?
        """
        modelo = result['modelo']

        # ── Validación de año ──
        if año:
            yr = self.catalog.get_year_range(marca, modelo)
            if yr:
                desde = yr.get('desde', 0)
                hasta = yr.get('hasta', 9999)

                if año < desde or año > hasta:
                    # El año no coincide → buscar sucesor/predecesor
                    suggestion = self.catalog.get_year_suggestion(marca, modelo, año)

                    if suggestion and self.catalog.has_model(marca, suggestion):
                        # Verificar que el sugerido sí cubra el año
                        sug_yr = self.catalog.get_year_range(marca, suggestion)
                        if sug_yr:
                            sug_desde = sug_yr.get('desde', 0)
                            sug_hasta = sug_yr.get('hasta', 9999)

                            if sug_desde <= año <= sug_hasta:
                                result['corrections'].append({
                                    'tipo': 'year_mismatch',
                                    'original': modelo,
                                    'corregido': suggestion,
                                    'razon': (
                                        f"'{modelo}' existió {desde}-{hasta}, "
                                        f"año {año} corresponde a '{suggestion}' "
                                        f"({sug_desde}-{sug_hasta})"
                                    )
                                })
                                result['modelo'] = suggestion
                                STATS.year_corrected += 1
                    else:
                        result['warnings'].append(
                            f"Año {año} fuera de rango para '{modelo}' "
                            f"({desde}-{hasta})"
                        )

        # ── Verificar confusiones ──
        confusions = self.catalog.get_confusions(marca, result['modelo'])
        if confusions:
            confusion_names = [c['modelo_confundido'] for c in confusions]
            result['warnings'].append(
                f"Modelo '{result['modelo']}' se confunde frecuentemente con: "
                f"{', '.join(confusion_names)}"
            )
            STATS.confusion_detected += 1

        # ── Resolver modelo padre ──
        parent_key = make_key(marca, result['modelo'])
        parent = self.catalog.model_parent_map.get(parent_key)
        if parent:
            result['warnings'].append(
                f"'{result['modelo']}' es variante de '{parent}'"
            )

    # ─────────────────────────────────────────────
    # CONFIDENCE SCORE
    # ─────────────────────────────────────────────

    def _calculate_confidence(self, result: Dict):
        """
        Calcula score de confianza (0-100).

        Match exacto catálogo + año válido     = 95-100
        Match exacto catálogo sin año          = 80-90
        Match por alias                        = 80-90
        Match por código mapeado               = 75-85
        Match por nombre base                  = 65-80
        Fuzzy con nombre largo                 = 55-70
        Fuzzy con nombre corto                 = 30-50
        Fallback                               = 10-20
        """
        score = 0
        method = result.get('match_method', 'fallback')

        # Base por método
        method_scores = {
            'exact': 90,
            'alias': 85,
            'code_mapping': 80,
            'base_name': 70,
            'fuzzy': 55,
            'fallback': 15,
        }
        score = method_scores.get(method, 10)

        # Bonus: tiene versión del catálogo
        if result.get('version') and result.get('from_catalog'):
            score += 5

        # Bonus: año detectado y dentro de rango
        if result.get('año_detectado') and result.get('from_catalog') and result.get('modelo'):
            yr = self.catalog.get_year_range(result['marca'], result['modelo'])
            if yr:
                desde = yr.get('desde', 0)
                hasta = yr.get('hasta', 9999)
                if desde <= result['año_detectado'] <= hasta:
                    score += 5  # Año confirmado

        # Penalización: tiene correcciones por año
        if result.get('corrections'):
            score -= 10

        # Penalización: tiene warnings de confusión
        if any('confunde' in w for w in result.get('warnings', [])):
            score -= 5

        # Penalización: modelo numérico con fuzzy
        if method == 'fuzzy' and result.get('modelo') and is_numeric_model(result['modelo']):
            score -= 20  # Esto no debería pasar, pero por seguridad

        # Limitar
        score = max(0, min(100, score))
        result['confidence'] = score

        # Nivel
        if score >= 80:
            result['confidence_level'] = 'high'
            STATS.by_confidence['high'] = STATS.by_confidence.get('high', 0) + 1
        elif score >= 60:
            result['confidence_level'] = 'medium'
            STATS.by_confidence['medium'] = STATS.by_confidence.get('medium', 0) + 1
        elif score >= 40:
            result['confidence_level'] = 'low'
            STATS.by_confidence['low'] = STATS.by_confidence.get('low', 0) + 1
        else:
            result['confidence_level'] = 'very_low'
            STATS.by_confidence['very_low'] = STATS.by_confidence.get('very_low', 0) + 1

    # ─────────────────────────────────────────────
    # STATUS FINAL
    # ─────────────────────────────────────────────

    def _set_final_status(self, result: Dict):
        if result['from_catalog']:
            if result['version']:
                result['norm_status'] = 'full_match'
                STATS.full_match += 1
            elif result['modelo']:
                result['norm_status'] = 'partial_match'
                STATS.partial_match += 1
            else:
                result['norm_status'] = 'marca_only'
                STATS.marca_only += 1
        else:
            if result.get('modelo'):
                result['norm_status'] = 'fallback'
            else:
                result['norm_status'] = 'no_modelo'
            STATS.fallback_used += 1


# Instancia global
NORMALIZER = VehicleNormalizerV4()


# ═══════════════════════════════════════════════════════════════
# 🚀 FUNCIONES PÚBLICAS (API)
# ═══════════════════════════════════════════════════════════════

def init_normalizer(
    dicts_path: str = None,
    brand_aliases_path: str = None
) -> bool:
    global CATALOG, NORMALIZER, STATS
    STATS = NormalizationStats()
    dicts_path = dicts_path or CONFIG.dicts_path
    brand_aliases_path = brand_aliases_path or CONFIG.brand_aliases_path
    loaded = CATALOG.load(dicts_path, brand_aliases_path)
    NORMALIZER = VehicleNormalizerV4(CATALOG)
    return loaded


def normalize_vehicle(
    titulo: str = "",
    descripcion: str = "",
    marca_raw: str = "",
    modelo_raw: str = "",
    version_raw: str = "",
    año_raw: Optional[int] = None,
    precio_raw: Optional[float] = None,
) -> Dict:
    return NORMALIZER.normalize(
        titulo=titulo,
        descripcion=descripcion,
        marca_raw=marca_raw,
        modelo_raw=modelo_raw,
        version_raw=version_raw,
        año_raw=año_raw,
        precio_raw=precio_raw,
    )


def get_stats() -> NormalizationStats:
    return STATS


def get_catalog() -> EnrichedCatalog:
    return CATALOG


# ═══════════════════════════════════════════════════════════════
# 🧪 PRUEBAS
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    print("=" * 70)
    print("🧪 PRUEBA DEL NORMALIZADOR v4.0")
    print("=" * 70)

    loaded = init_normalizer("config/normalizer_dicts.json")
    print(f"\n📚 Catálogo cargado: {loaded}")

    if not loaded:
        print("❌ No se pudo cargar el catálogo")
        exit(1)

    test_cases = [
        # ── P0: No fuzzy para numéricos ──
        {
            "name": "🔴 Peugeot 208 (no confundir con 206)",
            "titulo": "Peugeot 208 Allure 2015",
            "marca_raw": "Peugeot",
            "modelo_raw": "208",
            "expected": {"marca": "peugeot", "modelo": "208"}
        },
        # ── P1: Validación con año ──
        {
            "name": "🔴 Peugeot '208' año 2005 → debería ser 206",
            "titulo": "Peugeot 208 año 2005",
            "marca_raw": "Peugeot",
            "modelo_raw": "208",
            "expected": {"marca": "peugeot", "modelo": "206"}
        },
        # ── Código mapeado ──
        {
            "name": "Mercedes C200 → Clase C",
            "titulo": "Mercedes Benz C200 Año 2000 Impecable",
            "marca_raw": "Mercedes Benz",
            "version_raw": "C200",
            "expected": {"marca": "mercedes-benz", "modelo": "clase c"}
        },
        # ── BMW código ──
        {
            "name": "BMW 320i → Serie 3",
            "titulo": "BMW 320i Sport 2019",
            "marca_raw": "BMW",
            "version_raw": "320i Sport",
            "expected": {"marca": "bmw", "modelo": "serie 3"}
        },
        # ── Alfa Romeo alias ──
        {
            "name": "Alfa Romeo 145 (alias 'alfa 145')",
            "titulo": "Alfa 145 Twin Spark",
            "marca_raw": "Alfa Romeo",
            "modelo_raw": "145",
            "expected": {"marca": "alfa romeo", "modelo": "145"}
        },
        # ── P0: Motorización no es modelo ──
        {
            "name": "🔴 '2.0 TDI' no es modelo",
            "titulo": "Volkswagen Amarok 2.0 TDI Highline",
            "marca_raw": "Volkswagen",
            "modelo_raw": "2.0 TDI",
            "version_raw": "Highline",
            "expected": {"marca": "volkswagen", "modelo": "amarok"}
        },
        # ── Modelo exacto directo ──
        {
            "name": "VW Amarok (alias vw)",
            "titulo": "VW Amarok Highline 4x4",
            "marca_raw": "VW",
            "version_raw": "Amarok Highline",
            "expected": {"marca": "volkswagen", "modelo": "amarok"}
        },
        # ── BMW M3 como modelo independiente ──
        {
            "name": "BMW M3 (modelo independiente, no Serie 3)",
            "titulo": "BMW M3 Competition 2022",
            "marca_raw": "BMW",
            "modelo_raw": "M3",
            "expected": {"marca": "bmw", "modelo": "m3"}
        },
    ]

    print("\n" + "─" * 70)
    passed = 0
    failed = 0

    for test in test_cases:
        print(f"\n📋 {test['name']}")
        print(f"   Título: {test.get('titulo', '')}")
        print(f"   Marca raw: {test.get('marca_raw', '')}")
        print(f"   Modelo raw: {test.get('modelo_raw', '')}")
        print(f"   Version raw: {test.get('version_raw', '')}")

        result = normalize_vehicle(
            titulo=test.get('titulo', ''),
            descripcion=test.get('descripcion', ''),
            marca_raw=test.get('marca_raw', ''),
            modelo_raw=test.get('modelo_raw', ''),
            version_raw=test.get('version_raw', ''),
        )

        expected = test.get('expected', {})
        marca_ok = result['marca'] == expected.get('marca')
        modelo_ok = result['modelo'] == expected.get('modelo')
        version_ok = (
            result['version'] == expected.get('version')
            or 'version' not in expected
        )
        all_ok = marca_ok and modelo_ok and version_ok

        if all_ok:
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1

        print(f"\n   {status}")
        print(f"   marca: {result['marca']}", "✓" if marca_ok else f"✗ (esperado: {expected.get('marca')})")
        print(f"   modelo: {result['modelo']}", "✓" if modelo_ok else f"✗ (esperado: {expected.get('modelo')})")
        print(f"   version: {result['version']}")
        print(f"   method: {result.get('match_method')}")
        print(f"   confidence: {result['confidence']} ({result['confidence_level']})")
        print(f"   año: {result.get('año_detectado')}")
        if result.get('warnings'):
            print(f"   ⚠️ warnings: {result['warnings']}")
        if result.get('corrections'):
            print(f"   🔧 corrections: {result['corrections']}")

    print("\n" + "═" * 70)
    print(f"📊 RESUMEN: {passed} passed, {failed} failed")
    print(f"📈 Stats: {STATS.summary()}")
    print(f"📊 Por método: {dict(STATS.by_method)}")
    print(f"📊 Por confianza: {dict(STATS.by_confidence)}")
    print("=" * 70)
