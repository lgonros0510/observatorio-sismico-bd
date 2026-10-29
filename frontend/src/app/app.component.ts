import { CommonModule } from '@angular/common';
import { Component, OnDestroy, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LeafletDirective } from '@bluehalo/ngx-leaflet';
import * as L from 'leaflet';
import type { FeatureCollection, Point } from 'geojson';

type Properties = {
  mag: number | null;
  magType?: string | null;
  place: string | null;
  time: number;
  updated: number;
  country_id?: string | null;
  country_name?: string | null;
  test_case?: boolean;
};
type EventFeature = GeoJSON.Feature<Point, Properties>;
type Events = FeatureCollection<Point, Properties> & {
  metadata?: {
    generated?: number;
    last_ingestion?: number | null;
    mode?: string;
    total?: number;
    count?: number;
    limit?: number;
    offset?: number;
    truncated?: boolean;
    period?: { start: number; end: number };
  };
};
type Config = { mode: 'demo' | 'api'; apiUrl: string; refreshMs: number; skipNgrokWarning: boolean };
type CountRow = { country_id?: string | null; country_name?: string; bucket?: string; count: number };
type Statistics = {
  generated: number;
  last_ingestion: number | null;
  counts: {
    events: number;
    population_before_min_mag: number;
    without_magnitude: number;
    without_depth: number;
    without_country: number;
  };
  hourly_utc: { hour: string; count: number }[];
  by_country: CountRow[];
  magnitude_distribution: CountRow[];
  depth_distribution: CountRow[];
  historical_comparison: {
    historical_year: number;
    historical_total: number;
    historical_days: number;
    historical_average_per_day: number;
    recent_total: number;
    recent_period_hours: number;
    recent_per_day: number;
    relative_to_historical_daily_average: number | null;
    note: string;
  };
};
type Country = { country_id: string; name_es: string | null; admin_name: string | null; continent: string | null; subregion: string | null };

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule, LeafletDirective],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css'
})
export class AppComponent implements OnDestroy {
  readonly mode = signal('Cargando configuración');
  readonly status = signal('Preparando el mapa…');
  readonly error = signal('');
  readonly lastRead = signal('Sin lectura');
  readonly generated = signal('Sin información');
  readonly lastIngestion = signal('Sin ingestas registradas');
  readonly lastIngestionMs = signal<number | null>(null);
  readonly count = signal(0);
  readonly omitted = signal(0);
  readonly countriesStatus = signal('Cargando catálogo');
  readonly eventsData = signal<Events | null>(null);
  readonly statistics = signal<Statistics | null>(null);
  readonly countries = signal<Country[]>([]);
  readonly loading = signal(false);
  readonly hasMore = signal(false);
  readonly options: L.MapOptions = {
    center: L.latLng(18, 0), zoom: 2, minZoom: 2, maxZoom: 8, preferCanvas: true, worldCopyJump: false
  };
  viewMode: 'live' | 'replay' = 'live';
  startLocal = '';
  endLocal = '';
  minMagnitude = '';
  countryId = '';
  tableSearch = '';

  private map?: L.Map;
  private eventsLayer = L.layerGroup();
  private timer?: ReturnType<typeof setTimeout>;
  private controller = new AbortController();
  private destroyed = false;
  private config?: Config;
  private demoData?: Events;

  async onMapReady(map: L.Map): Promise<void> {
    this.map = map;
    const base = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, noWrap: true,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(map);
    const borders = L.geoJSON(undefined, {
      style: { color: '#334155', weight: 1, fill: false },
      onEachFeature: (feature, layer) => {
        const name = document.createElement('span');
        name.textContent = feature.properties?.NAME_ES ?? feature.properties?.ADMIN ?? 'Sin nombre';
        layer.bindTooltip(name);
      }
    }).addTo(map);
    this.eventsLayer.addTo(map);
    L.control.layers({ OpenStreetMap: base }, { 'Países (Natural Earth)': borders, Terremotos: this.eventsLayer }).addTo(map);
    map.attributionControl.addAttribution('Fronteras: Natural Earth · Datos: USGS');

    try {
      borders.addData(await this.readJson<FeatureCollection>('datos/paises.geojson'));
      this.countriesStatus.set('Catálogo de países cargado');
    } catch (error) {
      this.countriesStatus.set(`No se pudo cargar el catálogo: ${this.errorMessage(error)}`);
    }
    if (this.destroyed) return;

    try {
      const config = await this.readJson<Config>('config.json');
      if (!['demo', 'api'].includes(config.mode)) throw new Error('Modo inválido en config.json');
      if (config.mode === 'api' && !/^https:\/\//i.test(config.apiUrl)) {
        throw new Error('apiUrl debe usar HTTPS para la conexión pública');
      }
      this.config = config;
      this.mode.set(config.mode === 'demo' ? 'DEMOSTRACIÓN · datos ficticios' : 'API · datos del backend');
      const end = new Date();
      const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
      this.startLocal = this.toLocalInput(start);
      this.endLocal = this.toLocalInput(end);
      if (config.mode === 'api') {
        this.minMagnitude = '4';
        try {
          await this.loadCountries(config);
          this.countriesStatus.set(`${this.countries().length} países disponibles para filtrar`);
        } catch (error) {
          this.countriesStatus.set(`No se pudo cargar el filtro de países: ${this.errorMessage(error)}`);
        }
      }
      await this.refresh(config);
    } catch (error) {
      this.error.set(this.errorMessage(error));
      this.status.set('No se pudo iniciar el panel; revisa la configuración');
    }
  }

  get visibleEvents(): EventFeature[] {
    const features = this.eventsData()?.features ?? [];
    const search = this.tableSearch.trim().toLocaleLowerCase();
    if (!search) return features;
    return features.filter(feature =>
      [feature.id, feature.properties?.place, feature.properties?.country_name]
        .some(value => String(value ?? '').toLocaleLowerCase().includes(search))
    );
  }

  async applyFilters(): Promise<void> {
    if (this.config) await this.refresh(this.config);
  }

  async loadMore(): Promise<void> {
    const config = this.config;
    if (!config || config.mode !== 'api' || !this.hasMore()) return;
    clearTimeout(this.timer);
    this.loading.set(true);
    this.error.set('');
    try {
      await this.fetchPage(config, this.eventsData()?.features.length ?? 0, true);
    } catch (error) {
      this.error.set(this.errorMessage(error));
      this.status.set('No se pudo cargar la siguiente página; se conservan los eventos visibles');
    } finally {
      this.loading.set(false);
      if (!this.destroyed) {
        this.timer = setTimeout(
          () => void this.refresh(config),
          Math.max(30000, Number(config.refreshMs) || 60000)
        );
      }
    }
  }

  async changeMode(mode: 'live' | 'replay'): Promise<void> {
    this.viewMode = mode;
    if (this.config) await this.refresh(this.config);
  }

  maximum(rows: CountRow[]): number {
    return Math.max(1, ...rows.map(row => row.count));
  }

  barWidth(value: number, rows: CountRow[]): number {
    return Math.max(1, (value / this.maximum(rows)) * 100);
  }

  depthLabel(event: EventFeature): string | number {
    const depth = event.geometry.coordinates[2];
    return typeof depth === 'number' && Number.isFinite(depth) ? depth : 'ND';
  }

  hasDepth(event: EventFeature): boolean {
    return typeof event.geometry.coordinates[2] === 'number' && Number.isFinite(event.geometry.coordinates[2]);
  }

  conclusions(): string[] {
    const stats = this.statistics();
    if (!stats) return [];
    const firstCountry = stats.by_country[0];
    const comparison = stats.historical_comparison;
    const magnitudeCoverage = stats.counts.population_before_min_mag
      ? 100 * (stats.counts.population_before_min_mag - stats.counts.without_magnitude) / stats.counts.population_before_min_mag
      : 0;
    const output = [
      `En el periodo y ámbito seleccionados hay ${stats.counts.without_magnitude} eventos sin magnitud (${(100 - magnitudeCoverage).toFixed(1)} % de ${stats.counts.population_before_min_mag}); no contribuyen al filtro mínimo de magnitud.`,
      firstCountry
        ? `La categoría con más eventos es ${firstCountry.country_name} (${firstCountry.count}); ${stats.counts.without_country} eventos están sin país asignado.`
        : 'No hay eventos que cumplan los filtros actuales para comparar recuentos por país.',
      comparison.historical_average_per_day > 0
        ? `El ritmo observado es ${comparison.recent_per_day.toFixed(2)} eventos/día frente a ${comparison.historical_average_per_day.toFixed(2)} de media diaria en ${comparison.historical_year} bajo los mismos filtros.`
        : `La extracción histórica de ${comparison.historical_year} no contiene eventos con estos filtros; no se calcula una razón relativa.`
    ];
    return output;
  }

  isIngestionStale(): boolean {
    const last = this.lastIngestionMs();
    return this.config?.mode === 'api' && (last === null || Date.now() - last > 3 * 60 * 1000);
  }

  private async loadCountries(config: Config): Promise<void> {
    const response = await this.readJson<{ countries: Country[] }>(
      `${config.apiUrl.replace(/\/$/, '')}/api/paises`, this.apiHeaders(config)
    );
    this.countries.set(response.countries);
  }

  private async readJson<T>(url: string, headers: Record<string, string> = {}): Promise<T> {
    const response = await fetch(url, {
      headers,
      cache: 'no-store',
      signal: AbortSignal.any([this.controller.signal, AbortSignal.timeout(15000)])
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json() as Promise<T>;
  }

  private apiHeaders(config: Config): Record<string, string> {
    return config.skipNgrokWarning ? { 'ngrok-skip-browser-warning': '1' } : {};
  }

  private async refresh(config: Config): Promise<void> {
    clearTimeout(this.timer);
    if (config.mode === 'demo') {
      try {
        if (!this.demoData) {
          this.demoData = await this.readJson<Events>('datos/terremotos-demo.geojson');
          const times = this.demoData.features
            .map(feature => feature.properties?.time)
            .filter((time): time is number => typeof time === 'number' && Number.isFinite(time));
          if (times.length) {
            this.startLocal = this.toLocalInput(new Date(Math.min(...times)));
            this.endLocal = this.toLocalInput(new Date(Math.max(...times) + 60000));
          }
          const demoCountries = new Map<string, Country>();
          for (const feature of this.demoData.features) {
            const properties = feature.properties;
            if (properties?.country_id && properties.country_name) {
              demoCountries.set(properties.country_id, {
                country_id: properties.country_id,
                name_es: properties.country_name,
                admin_name: properties.country_name,
                continent: null,
                subregion: null
              });
            }
          }
          this.countries.set([...demoCountries.values()]);
        }
        this.applyDemoFilters();
      } catch (error) {
        this.error.set(this.errorMessage(error));
        this.status.set(this.demoData ? 'No se pudieron aplicar los filtros' : 'No se pudo cargar la demostración');
      }
      return;
    }
    this.loading.set(true);
    this.error.set('');
    try {
      const [page, statistics] = await Promise.all([
        this.requestEventPage(config, 0),
        this.requestStatistics(config)
      ]);
      this.commitEventPage(page, 0, false);
      this.statistics.set(statistics);
      this.lastIngestion.set(this.date(statistics.last_ingestion));
      this.lastIngestionMs.set(statistics.last_ingestion);
      this.lastRead.set(new Date().toISOString());
      this.status.set(this.hasMore()
        ? 'API accesible · resultados paginados; las estadísticas usan el conjunto completo'
        : 'API accesible · consulta completada');
    } catch (error) {
      this.error.set(this.errorMessage(error));
      this.status.set('No se pudo actualizar; se conserva la última lectura correcta');
    } finally {
      this.loading.set(false);
      if (!this.destroyed && config.mode === 'api') {
        this.timer = setTimeout(() => void this.refresh(config), Math.max(30000, Number(config.refreshMs) || 60000));
      }
    }
  }

  private applyDemoFilters(): void {
    const source = this.demoData;
    if (!source) return;
    const start = this.toIso(this.startLocal);
    const end = this.toIso(this.endLocal);
    if (start && end && start >= end) throw new Error('La fecha inicial debe ser anterior a la fecha final');
    const minimumInput = String(this.minMagnitude ?? '').trim();
    const minimum = minimumInput ? Number(minimumInput) : null;
    if (minimum !== null && (!Number.isFinite(minimum) || minimum < -10 || minimum > 10)) {
      throw new Error('La magnitud mínima debe ser un número entre -10 y 10');
    }
    const startMs = start ? Date.parse(start) : Number.NEGATIVE_INFINITY;
    const endMs = end ? Date.parse(end) : Number.POSITIVE_INFINITY;
    const features = source.features.filter(feature => {
      const properties = feature.properties;
      if (!properties || properties.time < startMs || properties.time >= endMs) return false;
      if (minimum !== null && (properties.mag === null || properties.mag < minimum)) return false;
      if (this.countryId === '__none__' && (properties.country_id || properties.country_name)) return false;
      if (this.countryId && this.countryId !== '__none__' && properties.country_id !== this.countryId) return false;
      return true;
    });
    const filtered: Events = {
      ...source,
      metadata: {
        ...source.metadata,
        total: features.length,
        count: features.length,
        truncated: false
      },
      features
    };
    this.eventsData.set(filtered);
    this.statistics.set(null);
    this.drawEvents(filtered);
    this.lastRead.set(new Date().toISOString());
    this.generated.set(this.date(source.metadata?.generated));
    this.status.set(`Demostración: ${features.length} de ${source.features.length} eventos ficticios según los filtros`);
  }

  private async fetchPage(config: Config, offset: number, append: boolean): Promise<void> {
    const page = await this.requestEventPage(config, offset);
    this.commitEventPage(page, offset, append);
  }

  private async requestEventPage(config: Config, offset: number): Promise<Events> {
    const params = new URLSearchParams({ mode: this.viewMode, limit: '500', offset: String(offset) });
    const start = this.toIso(this.startLocal);
    const end = this.toIso(this.endLocal);
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    const minimum = String(this.minMagnitude ?? '').trim();
    if (minimum) {
      const value = Number(minimum);
      if (!Number.isFinite(value)) throw new Error('La magnitud mínima debe ser un número');
      params.set('min_mag', String(value));
    }
    if (this.countryId) params.set('country_id', this.countryId);
    const url = `${config.apiUrl.replace(/\/$/, '')}/api/eventos?${params}`;
    const page = await this.readJson<Events>(url, this.apiHeaders(config));
    this.validateCollection(page);
    return page;
  }

  private commitEventPage(page: Events, offset: number, append: boolean): void {
    const previous = append ? this.eventsData()?.features ?? [] : [];
    const merged = { ...page, features: [...previous, ...page.features] } as Events;
    this.eventsData.set(merged);
    this.drawEvents(merged);
    this.hasMore.set(offset + page.features.length < (page.metadata?.total ?? page.features.length));
    this.generated.set(this.date(page.metadata?.generated));
    this.lastIngestion.set(this.date(page.metadata?.last_ingestion));
    this.lastIngestionMs.set(page.metadata?.last_ingestion ?? null);
  }

  private async requestStatistics(config: Config): Promise<Statistics> {
    const params = new URLSearchParams({ mode: this.viewMode });
    const start = this.toIso(this.startLocal);
    const end = this.toIso(this.endLocal);
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    const minimum = String(this.minMagnitude ?? '').trim();
    if (minimum) params.set('min_mag', minimum);
    if (this.countryId) params.set('country_id', this.countryId);
    return this.readJson<Statistics>(
      `${config.apiUrl.replace(/\/$/, '')}/api/estadisticas?${params}`, this.apiHeaders(config)
    );
  }

  private validateCollection(data: Events): void {
    if (data.type !== 'FeatureCollection' || !Array.isArray(data.features)) {
      throw new Error('La API no devolvió una FeatureCollection');
    }
  }

  private drawEvents(data: Events): void {
    if (!this.map) return;
    const next = L.layerGroup();
    let omitted = 0;
    let count = 0;
    for (const feature of data.features) {
      const properties = feature.properties;
      const coordinates = feature.geometry?.type === 'Point' ? feature.geometry.coordinates : [];
      const [longitude, latitude, depth] = coordinates;
      if (!properties || !Number.isFinite(longitude) || !Number.isFinite(latitude) ||
        Math.abs(longitude) > 180 || Math.abs(latitude) > 90) {
        omitted++;
        continue;
      }
      const magnitude = typeof properties.mag === 'number' && Number.isFinite(properties.mag)
        ? properties.mag : null;
      const validDepth = typeof depth === 'number' && Number.isFinite(depth);
      const color = !validDepth ? '#64748b' : depth < 70 ? '#e8a317' : depth < 300 ? '#e35b37' : '#7b3fb6';
      const popup = document.createElement('div');
      popup.textContent = [
        properties.place ?? 'Sin descripción',
        `Magnitud: ${magnitude ?? 'ND'} ${properties.magType ?? ''}`,
        `Profundidad: ${validDepth ? `${depth} km` : 'ND'}`,
        `Fecha UTC: ${this.date(properties.time)}`,
        `País: ${properties.country_name ?? 'Sin país asignado'}`,
        ...(properties.test_case ? ['CASO CONTROLADO DE PRUEBA'] : []),
        `ID: ${feature.id ?? 'ND'}`
      ].join('\n');
      L.circleMarker([latitude, longitude], {
        radius: magnitude === null ? 4 : Math.max(3, Math.min(18, 3 + (magnitude + 1) * 2)),
        color, fillColor: color, fillOpacity: 0.75, weight: 1
      }).bindPopup(popup).addTo(next);
      count++;
    }
    this.eventsLayer.clearLayers();
    this.eventsLayer.addLayer(next);
    this.count.set(count);
    this.omitted.set(omitted);
  }

  private toIso(value: string): string | null {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) throw new Error('Fecha de filtro inválida');
    return date.toISOString();
  }

  private toLocalInput(date: Date): string {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  private date(value: unknown): string {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 'ND';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'ND' : date.toISOString();
  }

  private errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : 'Error desconocido';
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    clearTimeout(this.timer);
    this.controller.abort();
  }
}
