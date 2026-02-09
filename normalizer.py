"""
🔧 Módulo de Normalización de Vehículos v2.3
============================================
Normaliza marca, modelo y versión comparando contra catálogo CSV.
Si no encuentra en catálogo, usa los datos originales extraídos.

Reglas:
- Marca: catálogo > raw (nunca vacía)
- Modelo: catálogo > extraído de version_raw/título (nunca vacío si hay marca)
- Versión: catálogo > NULL (opcional)
"""

import csv
import re
import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass

# Dependencias opcionales
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
            'Á': 'a', 'É': 'e', 'Í': 'i', 'Ó': 'o', 'Ú': 'u', 'Ñ': 'n',
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
# 📊 ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════
@dataclass
class NormalizationStats:
    total: int = 0
    full_match: int = 0       # marca + modelo + versión del catálogo
    partial_match: int = 0    # marca + modelo del catálogo (versión no)
    marca_only: int = 0       # solo marca del catálogo
    fallback_used: int = 0    # usó datos originales (no del catálogo)

    def summary(self) -> str:
        if self.total == 0:
            return "Sin datos"
        return (
            f"Total: {self.total} | "
            f"Full: {self.full_match} | "
            f"Parcial: {self.partial_match} | "
            f"Marca: {self.marca_only} | "
            f"Fallback: {self.fallback_used}"
        )


NORM_STATS = NormalizationStats()


# ═══════════════════════════════════════════════════════════════
# 🔧 CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
@dataclass
class NormalizerConfig:
    fuzzy_threshold_marca: int = 75
    fuzzy_threshold_modelo: int = 75
    fuzzy_threshold_version: int = 75
    enable_fuzzy: bool = True
    catalog_path: str = "config/vehicles_catalog.csv"
    aliases_path: str = "config/brand_aliases.json"


CONFIG = NormalizerConfig()


# ═══════════════════════════════════════════════════════════════
# 🔥 KEYWORDS DE URGENCIA
# ═══════════════════════════════════════════════════════════════
URGENCY_KEYWORDS = {
    'urgente': 30, 'urge': 30, 'urgencia': 30,
    'viajo': 25, 'viaje': 25, 'me voy': 25, 'mudanza': 25,
    'oportunidad': 20, 'negociable': 15, 'escucho': 15,
    'escucho ofertas': 20, 'acepto oferta': 20, 'ofertas': 15,
    'vendo ya': 20, 'venta rapida': 20, 'liquido': 20,
    'financio': 10, 'permuto': 10, 'contado': 10,
    'rebajado': 15, 'rebaja': 15,
    'precio final': -20, 'no negociable': -25, 'firme': -15,
}


# ═══════════════════════════════════════════════════════════════
# 📚 CATÁLOGO DE VEHÍCULOS
# ═══════════════════════════════════════════════════════════════
class VehicleCatalog:
    def __init__(self):
        self.catalog: Dict[str, Dict[str, Set[str]]] = {}
        self.brand_aliases: Dict[str, str] = {}
        self._all_brands: List[str] = []
        self._models_by_brand: Dict[str, List[str]] = {}
        self._loaded = False

    def load_from_csv(self, csv_path: str) -> bool:
        if not os.path.exists(csv_path):
            logger.warning(f"⚠️ Catálogo no encontrado: {csv_path}")
            return False

        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader, None)  # Skip header

                for row in reader:
                    if len(row) < 2:
                        continue

                    marca = self._normalize_key(row[0])
                    modelo = self._normalize_key(row[1])

                    if not marca or not modelo:
                        continue

                    if marca not in self.catalog:
                        self.catalog[marca] = {}
                    if modelo not in self.catalog[marca]:
                        self.catalog[marca][modelo] = set()

                    for version in row[2:]:
                        v = self._normalize_key(version)
                        if v:
                            self.catalog[marca][modelo].add(v)

            self._build_search_lists()
            self._loaded = True

            logger.info(
                f"✅ Catálogo: {len(self.catalog)} marcas, "
                f"{sum(len(m) for m in self.catalog.values())} modelos"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Error cargando catálogo: {e}")
            return False

    def load_aliases(self, aliases_path: str) -> bool:
        if not os.path.exists(aliases_path):
            return False
        try:
            with open(aliases_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for alias, brand in data.get('brand_aliases', {}).items():
                self.brand_aliases[self._normalize_key(alias)] = self._normalize_key(brand)
            logger.info(f"✅ Aliases: {len(self.brand_aliases)}")
            return True
        except:
            return False

    def _normalize_key(self, text: str) -> str:
        if not text:
            return ""
        text = text.lower().strip()
        text = unidecode(text)
        return ' '.join(text.split())

    def _build_search_lists(self):
        self._all_brands = sorted(self.catalog.keys(), key=len, reverse=True)
        self._models_by_brand = {
            marca: sorted(modelos.keys(), key=len, reverse=True)
            for marca, modelos in self.catalog.items()
        }

    def resolve_alias(self, brand: str) -> str:
        brand_norm = self._normalize_key(brand)
        return self.brand_aliases.get(brand_norm, brand_norm)

    def get_brands(self) -> List[str]:
        return self._all_brands.copy()

    def get_models(self, brand: str) -> List[str]:
        brand_norm = self.resolve_alias(self._normalize_key(brand))
        return self._models_by_brand.get(brand_norm, [])

    def get_versions(self, brand: str, model: str) -> List[str]:
        brand_norm = self.resolve_alias(self._normalize_key(brand))
        model_norm = self._normalize_key(model)
        versions = self.catalog.get(brand_norm, {}).get(model_norm, set())
        return sorted(versions, key=len, reverse=True)

    def is_loaded(self) -> bool:
        return self._loaded and len(self.catalog) > 0


CATALOG = VehicleCatalog()


# ═══════════════════════════════════════════════════════════════
# 🔍 FUNCIONES DE BÚSQUEDA
# ═══════════════════════════════════════════════════════════════
def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower().strip()
    text = unidecode(text)
    text = re.sub(r'[^\w\s]', ' ', text)
    return ' '.join(text.split())


def find_in_text(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    pattern = r'\b' + re.escape(needle) + r'\b'
    return bool(re.search(pattern, haystack))


def fuzzy_find_best(needle: str, candidates: List[str], threshold: int = 80) -> Optional[Tuple[str, int]]:
    if not HAS_RAPIDFUZZ or not needle or not candidates:
        return None
    try:
        result = process.extractOne(needle, candidates, scorer=fuzz.ratio, score_cutoff=threshold)
        return (result[0], result[1]) if result else None
    except:
        return None


def extract_first_word(text: str) -> str:
    """Extrae la primera palabra significativa (3+ caracteres)."""
    if not text:
        return ""
    words = normalize_text(text).split()
    for word in words:
        if len(word) >= 3 and not word.isdigit():
            return word
    return words[0] if words else ""


def extract_model_from_text(text: str, marca: str = "") -> str:
    """
    Extrae el modelo del texto, excluyendo la marca si está presente.
    """
    if not text:
        return ""
    
    text_norm = normalize_text(text)
    marca_norm = normalize_text(marca)
    
    # Remover la marca del texto si está presente
    if marca_norm and text_norm.startswith(marca_norm):
        text_norm = text_norm[len(marca_norm):].strip()
    
    # La primera palabra suele ser el modelo
    words = text_norm.split()
    if not words:
        return ""
    
    # Filtrar palabras que son años o muy cortas
    for word in words:
        if len(word) >= 2 and not re.match(r'^(19|20)\d{2}$', word):
            return word
    
    return words[0] if words else ""


# ═══════════════════════════════════════════════════════════════
# 🎯 NORMALIZADOR PRINCIPAL
# ═══════════════════════════════════════════════════════════════
class VehicleNormalizer:
    def __init__(self, catalog: VehicleCatalog = None):
        self.catalog = catalog or CATALOG
        self._cache: Dict[str, Dict] = {}

    def normalize(
        self,
        titulo: str = "",
        descripcion: str = "",
        marca_raw: str = "",
        modelo_raw: str = "",
        version_raw: str = ""
    ) -> Dict:
        """
        Normaliza un vehículo.
        
        Prioridad:
        1. Buscar en catálogo
        2. Si no encuentra, usar datos originales
        
        NUNCA retorna marca vacía ni marca sin modelo.
        """
        global NORM_STATS
        NORM_STATS.total += 1

        # Textos normalizados
        marca_norm = normalize_text(marca_raw)
        version_norm = normalize_text(version_raw)
        titulo_norm = normalize_text(titulo)
        desc_norm = normalize_text(descripcion[:500] if descripcion else "")
        
        # Texto completo para búsqueda
        full_text = f"{marca_norm} {titulo_norm} {version_norm} {desc_norm}"

        result = {
            'marca': None,
            'modelo': None,
            'version': None,
            'norm_status': 'fallback',
            'from_catalog': False
        }

        # ═══════════════════════════════════════════════════════
        # PASO 1: BUSCAR MARCA
        # ═══════════════════════════════════════════════════════
        marca_catalog = self._find_marca_in_catalog(full_text, marca_norm)
        
        if marca_catalog:
            result['marca'] = marca_catalog
            result['from_catalog'] = True
        elif marca_norm:
            # Fallback: usar marca original
            result['marca'] = marca_norm
        else:
            # Intentar extraer del título
            result['marca'] = extract_first_word(titulo_norm)
        
        # Si no hay marca, no podemos continuar
        if not result['marca']:
            NORM_STATS.fallback_used += 1
            result['norm_status'] = 'no_data'
            return result

        # ═══════════════════════════════════════════════════════
        # PASO 2: BUSCAR MODELO
        # ═══════════════════════════════════════════════════════
        modelo_catalog = None
        if marca_catalog:
            # Solo buscar en catálogo si la marca es del catálogo
            modelo_catalog = self._find_modelo_in_catalog(
                full_text, 
                marca_catalog,
                version_norm
            )
        
        if modelo_catalog:
            result['modelo'] = modelo_catalog
        else:
            # Fallback: extraer modelo de version_raw o título
            modelo_fallback = extract_model_from_text(version_raw, result['marca'])
            if not modelo_fallback:
                modelo_fallback = extract_model_from_text(titulo, result['marca'])
            
            result['modelo'] = modelo_fallback if modelo_fallback else None

        # Si hay marca pero no modelo, usar la primera palabra de version_raw
        if result['marca'] and not result['modelo'] and version_norm:
            words = version_norm.split()
            if words:
                result['modelo'] = words[0]

        # ═══════════════════════════════════════════════════════
        # PASO 3: BUSCAR VERSIÓN (opcional)
        # ═══════════════════════════════════════════════════════
        if marca_catalog and modelo_catalog:
            version_catalog = self._find_version_in_catalog(
                full_text,
                marca_catalog,
                modelo_catalog
            )
            if version_catalog:
                result['version'] = version_catalog

        # ═══════════════════════════════════════════════════════
        # DETERMINAR STATUS
        # ═══════════════════════════════════════════════════════
        if marca_catalog and modelo_catalog and result['version']:
            result['norm_status'] = 'full_match'
            NORM_STATS.full_match += 1
        elif marca_catalog and modelo_catalog:
            result['norm_status'] = 'partial_match'
            NORM_STATS.partial_match += 1
        elif marca_catalog:
            result['norm_status'] = 'marca_only'
            NORM_STATS.marca_only += 1
        else:
            result['norm_status'] = 'fallback'
            NORM_STATS.fallback_used += 1

        return result

    def _find_marca_in_catalog(self, full_text: str, marca_raw: str) -> Optional[str]:
        """Busca la marca en el catálogo."""
        if not self.catalog.is_loaded():
            return None

        # 1. Verificar marca_raw directamente (con alias)
        if marca_raw:
            marca_resolved = self.catalog.resolve_alias(marca_raw)
            if marca_resolved in self.catalog.catalog:
                return marca_resolved

        # 2. Buscar en el texto completo
        for brand in self.catalog.get_brands():
            if find_in_text(brand, full_text):
                return brand

        # 3. Fuzzy matching
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ and marca_raw:
            match = fuzzy_find_best(marca_raw, self.catalog.get_brands(), CONFIG.fuzzy_threshold_marca)
            if match:
                return match[0]

        return None

    def _find_modelo_in_catalog(self, full_text: str, marca: str, version_text: str = "") -> Optional[str]:
        """Busca el modelo en el catálogo."""
        modelos = self.catalog.get_models(marca)
        if not modelos:
            return None

        # Combinar textos para buscar
        search_text = f"{version_text} {full_text}"

        # 1. Buscar en el texto
        for model in modelos:
            if find_in_text(model, search_text):
                return model

        # 2. Fuzzy matching con palabras del texto
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ:
            words = [w for w in search_text.split() if len(w) >= 3]
            for word in words:
                match = fuzzy_find_best(word, modelos, CONFIG.fuzzy_threshold_modelo)
                if match:
                    return match[0]

        return None

    def _find_version_in_catalog(self, full_text: str, marca: str, modelo: str) -> Optional[str]:
        """Busca la versión en el catálogo."""
        versiones = self.catalog.get_versions(marca, modelo)
        if not versiones:
            return None

        # 1. Buscar en el texto
        for version in versiones:
            if find_in_text(version, full_text):
                return version

        # 2. Fuzzy matching
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ:
            words = [w for w in full_text.split() if len(w) >= 2]
            for word in words:
                match = fuzzy_find_best(word, versiones, CONFIG.fuzzy_threshold_version)
                if match:
                    return match[0]

        return None

    def clear_cache(self):
        self._cache.clear()


NORMALIZER = VehicleNormalizer()


# ═══════════════════════════════════════════════════════════════
# 🔥 DETECCIÓN DE URGENCIA
# ═══════════════════════════════════════════════════════════════
def extract_urgency_signals(text: str) -> Dict:
    if not text:
        return {'has_urgency': False, 'score': 0, 'keywords_found': []}

    text_lower = text.lower()
    text_normalized = normalize_text(text)

    score = 0
    keywords_found = []

    for keyword, points in URGENCY_KEYWORDS.items():
        if find_in_text(keyword, text_normalized) or keyword in text_lower:
            score += points
            if points > 0:
                keywords_found.append(keyword)

    score = max(0, min(100, score))

    return {
        'has_urgency': score >= 15,
        'score': score,
        'keywords_found': keywords_found
    }


# ═══════════════════════════════════════════════════════════════
# 🚀 FUNCIONES PÚBLICAS
# ═══════════════════════════════════════════════════════════════
def init_normalizer(catalog_path: str = None, aliases_path: str = None) -> bool:
    global CATALOG, NORMALIZER, NORM_STATS
    NORM_STATS = NormalizationStats()

    catalog_path = catalog_path or CONFIG.catalog_path
    aliases_path = aliases_path or CONFIG.aliases_path

    catalog_loaded = CATALOG.load_from_csv(catalog_path)
    CATALOG.load_aliases(aliases_path)
    NORMALIZER = VehicleNormalizer(CATALOG)

    return catalog_loaded


def normalize_vehicle(
    titulo: str = "",
    descripcion: str = "",
    marca_raw: str = "",
    modelo_raw: str = "",
    version_raw: str = ""
) -> Dict:
    return NORMALIZER.normalize(
        titulo=titulo,
        descripcion=descripcion,
        marca_raw=marca_raw,
        modelo_raw=modelo_raw,
        version_raw=version_raw
    )


def get_normalization_stats() -> NormalizationStats:
    return NORM_STATS


# ═══════════════════════════════════════════════════════════════
# 🧪 PRUEBAS
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("🧪 PRUEBA DEL NORMALIZADOR v2.3")
    print("=" * 60)

    init_normalizer()

    test_cases = [
        # Caso 1: Match completo
        {
            "titulo": "VOLKSWAGEN AMAROK 4X2",
            "marca_raw": "Volkswagen",
            "version_raw": "Amarok Highline",
        },
        # Caso 2: Marca en catálogo, modelo no
        {
            "titulo": "Chevrolet Vectra GT",
            "marca_raw": "Chevrolet",
            "version_raw": "Vectra GT 2.0",  # Vectra probablemente no esté en catálogo
        },
        # Caso 3: Nada en catálogo, usar fallback
        {
            "titulo": "Peugeot 504 SRD",
            "marca_raw": "Peugeot",
            "version_raw": "504 SRD",  # Modelo viejo, no está en catálogo
        },
        # Caso 4: Marca con alias
        {
            "titulo": "VW Gol Trend Pack III",
            "marca_raw": "VW",
            "version_raw": "Gol Trend Pack III",
        },
    ]

    for i, test in enumerate(test_cases, 1):
        print(f"\n--- Caso {i} ---")
        print(f"Título: {test['titulo']}")
        print(f"Marca raw: {test['marca_raw']}")
        print(f"Version raw: {test.get('version_raw', '')}")

        result = normalize_vehicle(**test)
        print(f"✅ Resultado:")
        print(f"   marca: {result['marca']}")
        print(f"   modelo: {result['modelo']}")
        print(f"   version: {result['version']}")
        print(f"   status: {result['norm_status']}")
        print(f"   from_catalog: {result['from_catalog']}")

    print(f"\n📊 Stats: {NORM_STATS.summary()}")
