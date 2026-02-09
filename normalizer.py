"""
🔧 Módulo de Normalización de Vehículos v2.2
============================================
Normaliza marca, modelo y versión comparando contra catálogo CSV.
Usa texto de título + marca + modelo + versión + descripción para mejor precisión.
Todo el output es en MINÚSCULAS.
"""

import csv
import re
import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass

# Manejo de dependencias opcionales
try:
    from unidecode import unidecode
    HAS_UNIDECODE = True
except ImportError:
    HAS_UNIDECODE = False
    def unidecode(text: str) -> str:
        """Fallback básico si unidecode no está instalado."""
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
# 📊 ESTADÍSTICAS DE NORMALIZACIÓN
# ═══════════════════════════════════════════════════════════════
@dataclass
class NormalizationStats:
    total: int = 0
    full_match: int = 0       # marca + modelo + versión
    partial_match: int = 0    # marca + modelo
    marca_only: int = 0       # solo marca
    no_match: int = 0         # nada encontrado
    fuzzy_used: int = 0       # veces que se usó fuzzy

    def summary(self) -> str:
        if self.total == 0:
            return "Sin datos de normalización"
        full_pct = self.full_match * 100 // self.total
        partial_pct = self.partial_match * 100 // self.total
        return (
            f"Total: {self.total} | "
            f"Completo: {self.full_match} ({full_pct}%) | "
            f"Parcial: {self.partial_match} ({partial_pct}%) | "
            f"Solo marca: {self.marca_only} | "
            f"Sin match: {self.no_match}"
        )


NORM_STATS = NormalizationStats()


# ═══════════════════════════════════════════════════════════════
# 🔧 CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
@dataclass
class NormalizerConfig:
    fuzzy_threshold_marca: int = 85
    fuzzy_threshold_modelo: int = 80
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
    'vendo ya': 20, 'venta rapida': 20,
    'financio': 10, 'permuto': 10, 'contado': 10,
    'rebajado': 15, 'rebaja': 15, 'liquido': 20,
    'precio final': -20, 'no negociable': -25, 'firme': -15,
}


# ═══════════════════════════════════════════════════════════════
# 📚 CATÁLOGO DE VEHÍCULOS
# ═══════════════════════════════════════════════════════════════
class VehicleCatalog:
    """
    Catálogo jerárquico de marcas -> modelos -> versiones.
    Cargado desde CSV.
    """

    def __init__(self):
        self.catalog: Dict[str, Dict[str, Set[str]]] = {}
        self.brand_aliases: Dict[str, str] = {}
        self._all_brands: List[str] = []
        self._models_by_brand: Dict[str, List[str]] = {}
        self._loaded = False

    def load_from_csv(self, csv_path: str) -> bool:
        """
        Carga el catálogo desde CSV.
        Formato: Marca,Modelo,Version1,Version2,...
        """
        if not os.path.exists(csv_path):
            logger.warning(f"⚠️ Catálogo no encontrado: {csv_path}")
            return False

        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)  # Saltar header

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

                    # Agregar versiones (columnas 3+)
                    for version in row[2:]:
                        version_norm = self._normalize_key(version)
                        if version_norm:
                            self.catalog[marca][modelo].add(version_norm)

            self._build_search_lists()
            self._loaded = True

            total_marcas = len(self.catalog)
            total_modelos = sum(len(m) for m in self.catalog.values())
            total_versiones = sum(
                len(v) for m in self.catalog.values() for v in m.values()
            )

            logger.info(
                f"✅ Catálogo cargado: {total_marcas} marcas, "
                f"{total_modelos} modelos, {total_versiones} versiones"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Error cargando catálogo: {e}")
            return False

    def load_aliases(self, aliases_path: str) -> bool:
        """Carga aliases de marcas desde JSON."""
        if not os.path.exists(aliases_path):
            return False

        try:
            with open(aliases_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            aliases = data.get('brand_aliases', {})
            for alias, brand in aliases.items():
                alias_norm = self._normalize_key(alias)
                brand_norm = self._normalize_key(brand)
                if alias_norm and brand_norm:
                    self.brand_aliases[alias_norm] = brand_norm

            logger.info(f"✅ Aliases cargados: {len(self.brand_aliases)}")
            return True

        except Exception as e:
            logger.error(f"❌ Error cargando aliases: {e}")
            return False

    def _normalize_key(self, text: str) -> str:
        """Normaliza texto para usar como clave."""
        if not text:
            return ""
        text = text.lower().strip()
        text = unidecode(text)
        text = ' '.join(text.split())
        return text

    def _build_search_lists(self):
        """Construye listas ordenadas para búsqueda."""
        # Marcas ordenadas por longitud (desc) para matchear primero las más largas
        self._all_brands = sorted(
            self.catalog.keys(),
            key=lambda x: len(x),
            reverse=True
        )

        self._models_by_brand = {}
        for marca, modelos in self.catalog.items():
            self._models_by_brand[marca] = sorted(
                modelos.keys(),
                key=lambda x: len(x),
                reverse=True
            )

    def resolve_alias(self, brand: str) -> str:
        """Resuelve un alias de marca a la marca canónica."""
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


# Instancia global
CATALOG = VehicleCatalog()


# ═══════════════════════════════════════════════════════════════
# 🔍 FUNCIONES DE BÚSQUEDA
# ═══════════════════════════════════════════════════════════════
def normalize_text(text: str) -> str:
    """Normaliza texto para comparación."""
    if not text:
        return ""
    text = text.lower().strip()
    text = unidecode(text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = ' '.join(text.split())
    return text


def find_in_text(needle: str, haystack: str) -> bool:
    """Busca una cadena como palabra completa en un texto."""
    if not needle or not haystack:
        return False
    pattern = r'\b' + re.escape(needle) + r'\b'
    return bool(re.search(pattern, haystack))


def fuzzy_find_best(
    needle: str,
    candidates: List[str],
    threshold: int = 80
) -> Optional[Tuple[str, int]]:
    """Encuentra el mejor match fuzzy."""
    if not HAS_RAPIDFUZZ or not needle or not candidates:
        return None

    try:
        result = process.extractOne(
            needle,
            candidates,
            scorer=fuzz.ratio,
            score_cutoff=threshold
        )
        if result:
            return (result[0], result[1])
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 🎯 NORMALIZADOR PRINCIPAL
# ═══════════════════════════════════════════════════════════════
class VehicleNormalizer:
    """
    Normaliza marca, modelo y versión de vehículos.
    Usa múltiples fuentes de texto para encontrar la mejor coincidencia.
    """

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
        Normaliza un vehículo comparando contra el catálogo.

        Args:
            titulo: Título de la publicación
            descripcion: Descripción ampliada
            marca_raw: Marca extraída de la página
            modelo_raw: Modelo extraído (puede estar vacío o incorrecto)
            version_raw: Versión extraída (en rosariogarage suele contener modelo+versión)

        Returns:
            Dict con marca, modelo, version normalizados (o None si no se encontró)
        """
        global NORM_STATS
        NORM_STATS.total += 1

        # Si el catálogo no está cargado, retornar sin normalizar
        if not self.catalog.is_loaded():
            return {
                'marca': None,
                'modelo': None,
                'version': None,
                'norm_status': 'no_catalog',
                'confidence': 0
            }

        # Construir texto completo para búsqueda
        # Prioridad: marca_raw > titulo > version_raw > descripcion
        all_texts = [
            normalize_text(marca_raw),
            normalize_text(titulo),
            normalize_text(version_raw),
            normalize_text(modelo_raw),
            normalize_text(descripcion[:500] if descripcion else "")  # Limitar descripción
        ]
        full_text = ' '.join(filter(None, all_texts))

        # Cache key
        cache_key = full_text[:300]
        if cache_key in self._cache:
            return self._cache[cache_key].copy()

        result = {
            'marca': None,
            'modelo': None,
            'version': None,
            'norm_status': 'no_match',
            'confidence': 0
        }

        # ═══════════════════════════════════════════════════════
        # PASO 1: Buscar marca
        # ═══════════════════════════════════════════════════════
        marca_found = self._find_marca(full_text, marca_raw)

        if not marca_found:
            NORM_STATS.no_match += 1
            self._cache[cache_key] = result
            return result

        result['marca'] = marca_found['value']
        result['confidence'] += 40

        # ═══════════════════════════════════════════════════════
        # PASO 2: Buscar modelo
        # ═══════════════════════════════════════════════════════
        # Usar version_raw como fuente principal (en rosariogarage contiene modelo+versión)
        modelo_source = f"{version_raw} {modelo_raw} {titulo}"
        modelo_found = self._find_modelo(
            normalize_text(modelo_source) + ' ' + full_text,
            marca_found['value']
        )

        if not modelo_found:
            NORM_STATS.marca_only += 1
            result['norm_status'] = 'marca_only'
            self._cache[cache_key] = result
            return result

        result['modelo'] = modelo_found['value']
        result['confidence'] += 35

        # ═══════════════════════════════════════════════════════
        # PASO 3: Buscar versión
        # ═══════════════════════════════════════════════════════
        version_found = self._find_version(
            full_text,
            marca_found['value'],
            modelo_found['value']
        )

        if version_found:
            result['version'] = version_found['value']
            result['confidence'] += 25
            result['norm_status'] = 'full_match'
            NORM_STATS.full_match += 1
        else:
            result['norm_status'] = 'partial_match'
            NORM_STATS.partial_match += 1

        # Guardar en cache
        if len(self._cache) < 10000:
            self._cache[cache_key] = result

        return result

    def _find_marca(self, full_text: str, marca_raw: str) -> Optional[Dict]:
        """Busca la marca en el texto."""
        
        # 1. Verificar marca_raw directamente (con alias)
        marca_norm = normalize_text(marca_raw)
        if marca_norm:
            marca_resolved = self.catalog.resolve_alias(marca_norm)
            if marca_resolved in self.catalog.catalog:
                return {'value': marca_resolved, 'fuzzy': False}

        # 2. Buscar marcas del catálogo en el texto completo
        for brand in self.catalog.get_brands():
            if find_in_text(brand, full_text):
                return {'value': brand, 'fuzzy': False}

        # 3. Fuzzy matching
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ and marca_norm:
            match = fuzzy_find_best(
                marca_norm,
                self.catalog.get_brands(),
                CONFIG.fuzzy_threshold_marca
            )
            if match:
                NORM_STATS.fuzzy_used += 1
                return {'value': match[0], 'fuzzy': True, 'score': match[1]}

        return None

    def _find_modelo(self, full_text: str, marca: str) -> Optional[Dict]:
        """Busca el modelo en el texto."""
        modelos = self.catalog.get_models(marca)
        if not modelos:
            return None

        # 1. Buscar modelos en el texto (primero los más largos)
        for model in modelos:
            if find_in_text(model, full_text):
                return {'value': model, 'fuzzy': False}

        # 2. Fuzzy matching con palabras del texto
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ:
            # Probar con cada palabra del texto que tenga 3+ caracteres
            words = [w for w in full_text.split() if len(w) >= 3]
            for word in words:
                match = fuzzy_find_best(
                    word,
                    modelos,
                    CONFIG.fuzzy_threshold_modelo
                )
                if match:
                    NORM_STATS.fuzzy_used += 1
                    return {'value': match[0], 'fuzzy': True, 'score': match[1]}

        return None

    def _find_version(
        self,
        full_text: str,
        marca: str,
        modelo: str
    ) -> Optional[Dict]:
        """Busca la versión en el texto."""
        versiones = self.catalog.get_versions(marca, modelo)
        if not versiones:
            return None

        # 1. Buscar versiones en el texto (primero las más largas)
        for version in versiones:
            if find_in_text(version, full_text):
                return {'value': version, 'fuzzy': False}

        # 2. Fuzzy matching
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ:
            words = [w for w in full_text.split() if len(w) >= 2]
            for word in words:
                match = fuzzy_find_best(
                    word,
                    versiones,
                    CONFIG.fuzzy_threshold_version
                )
                if match:
                    NORM_STATS.fuzzy_used += 1
                    return {'value': match[0], 'fuzzy': True, 'score': match[1]}

        return None

    def clear_cache(self):
        """Limpia el cache."""
        self._cache.clear()


# Instancia global
NORMALIZER = VehicleNormalizer()


# ═══════════════════════════════════════════════════════════════
# 🔥 DETECCIÓN DE URGENCIA
# ═══════════════════════════════════════════════════════════════
def extract_urgency_signals(text: str) -> Dict:
    """
    Extrae señales de urgencia del texto.
    """
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
# 🚀 FUNCIONES DE INICIALIZACIÓN
# ═══════════════════════════════════════════════════════════════
def init_normalizer(
    catalog_path: str = None,
    aliases_path: str = None
) -> bool:
    """
    Inicializa el normalizador cargando catálogo y aliases.
    """
    global CATALOG, NORMALIZER, NORM_STATS

    NORM_STATS = NormalizationStats()

    catalog_path = catalog_path or CONFIG.catalog_path
    aliases_path = aliases_path or CONFIG.aliases_path

    # Cargar catálogo
    catalog_loaded = CATALOG.load_from_csv(catalog_path)

    # Cargar aliases (opcional)
    CATALOG.load_aliases(aliases_path)

    # Reinicializar normalizador
    NORMALIZER = VehicleNormalizer(CATALOG)

    return catalog_loaded


def normalize_vehicle(
    titulo: str = "",
    descripcion: str = "",
    marca_raw: str = "",
    modelo_raw: str = "",
    version_raw: str = ""
) -> Dict:
    """
    Función de conveniencia para normalizar un vehículo.
    """
    return NORMALIZER.normalize(
        titulo=titulo,
        descripcion=descripcion,
        marca_raw=marca_raw,
        modelo_raw=modelo_raw,
        version_raw=version_raw
    )


def get_normalization_stats() -> NormalizationStats:
    """Retorna las estadísticas de normalización."""
    return NORM_STATS


# ═══════════════════════════════════════════════════════════════
# 🧪 PRUEBAS
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("🧪 PRUEBA DEL NORMALIZADOR v2.2")
    print("=" * 60)

    if init_normalizer():
        test_cases = [
            {
                "titulo": "VOLKSWAGEN AMAROK 4X2",
                "marca_raw": "Volkswagen",
                "version_raw": "Amarok Highline",
                "descripcion": "Vendo urgente amarok highline 4x2, excelente estado"
            },
            {
                "titulo": "Chevrolet Onix 2019",
                "marca_raw": "Chevrolet",
                "version_raw": "LTZ",
                "descripcion": "Onix LTZ full, único dueño"
            },
            {
                "titulo": "Fiat Cronos 2020",
                "marca_raw": "Fiat",
                "version_raw": "Precision",
                "descripcion": "Cronos precision 1.8 automatico"
            },
            {
                "titulo": "VW Gol Trend",
                "marca_raw": "VW",
                "version_raw": "Pack III",
                "descripcion": "Gol trend pack 3, muy cuidado"
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

            # Probar urgencia
            urgency = extract_urgency_signals(test.get('descripcion', ''))
            print(f"   urgencia: {urgency['has_urgency']} (score: {urgency['score']})")

        print(f"\n📊 Estadísticas: {NORM_STATS.summary()}")
    else:
        print("❌ No se pudo inicializar el normalizador")
        print("   Asegúrate de tener: config/vehicles_catalog.csv")
