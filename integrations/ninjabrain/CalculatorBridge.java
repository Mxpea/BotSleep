// SPDX-License-Identifier: GPL-3.0-only
// Adapter to the unmodified Ninjabrain Bot 1.5.2 calculator. See third_party/ninjabrain/NOTICE.md.
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;
import ninjabrainbot.event.ArrayListImplementingReadOnlyList;
import ninjabrainbot.io.preferences.enums.McVersion;
import ninjabrainbot.model.datastate.blind.BlindPosition;
import ninjabrainbot.model.datastate.divine.Fossil;
import ninjabrainbot.model.datastate.divine.IDivineContext;
import ninjabrainbot.model.datastate.endereye.EnderEyeThrow;
import ninjabrainbot.model.datastate.endereye.EnderEyeThrowType;
import ninjabrainbot.model.datastate.endereye.IEnderEyeThrow;
import ninjabrainbot.model.datastate.statistics.Posterior;
import ninjabrainbot.model.datastate.stronghold.Chunk;
import ninjabrainbot.model.domainmodel.IDataComponent;
import ninjabrainbot.model.environmentstate.StandardDeviationSettings;
import ninjabrainbot.util.Logger;

public final class CalculatorBridge {
    // API rays already come from coordinates. Do not apply the GUI's crosshair or
    // F3+C rounding correction, and preserve a separate uncertainty per throw.
    private static final class CoordinateThrow extends EnderEyeThrow {
        final double sigma;
        CoordinateThrow(JSONObject data) {
            super(data.getDouble("x"), data.getDouble("z"), data.getDouble("bearing_deg"), 0, 0, 0);
            sigma = data.getDouble("sigma_deg");
        }
        public double getStandardDeviation(StandardDeviationSettings settings) { return sigma; }
        public double getExpectedStandardDeviationForNextEnderEyeThrow(StandardDeviationSettings settings) { return sigma; }
        public IEnderEyeThrow withCorrection(double correction, int increments) {
            if (correction != 0 || increments != 0) throw new UnsupportedOperationException("coordinate rays have no visual correction");
            return this;
        }
        public IEnderEyeThrow withToggledAltStd() { return this; }
        public EnderEyeThrowType getType() { return EnderEyeThrowType.MANUAL; }
    }

    private static final IDivineContext NO_DIVINE = new IDivineContext() {
        public Fossil getFossil() { return null; }
        public IDataComponent<Fossil> fossil() { return null; }
        public boolean hasDivine() { return false; }
        public double relativeDensity() { return 1; }
        public double getDensityAtAngleBeforeSnapping(double angle) { return 1 / (2 * Math.PI); }
        public BlindPosition getClosestCoords(double x, double z, double radius) {
            throw new UnsupportedOperationException("blind/divine mode is not used");
        }
    };

    public static void main(String[] args) {
        try {
            Logger.enabled = false;
            JSONObject input = new JSONObject(new String(System.in.readAllBytes(), StandardCharsets.UTF_8));
            ArrayListImplementingReadOnlyList<IEnderEyeThrow> throwsList = new ArrayListImplementingReadOnlyList<>();
            for (Object value : input.getJSONArray("throws")) throwsList.add(new CoordinateThrow((JSONObject) value));
            Posterior posterior = new Posterior(throwsList, NO_DIVINE,
                new StandardDeviationSettings(0.1, 0.1, 0.1, 0.1), input.optBoolean("advanced", true), McVersion.POST_119);
            List<Chunk> chunks = new ArrayList<>();
            double total = 0;
            for (Chunk chunk : posterior.getChunks()) {
                if (Double.isFinite(chunk.weight) && chunk.weight > 0) {
                    chunks.add(chunk);
                    total += chunk.weight;
                }
            }
            if (chunks.isEmpty() || !Double.isFinite(total) || total <= 0)
                throw new IllegalArgumentException("no consistent stronghold candidates; check rays and uncertainty");
            chunks.sort(Comparator.comparingDouble((Chunk chunk) -> chunk.weight).reversed());
            JSONArray candidates = new JSONArray();
            IEnderEyeThrow latest = throwsList.get(throwsList.size() - 1);
            int count = Math.min(input.optInt("top_k", 5), chunks.size());
            for (int i = 0; i < count; i++) {
                Chunk chunk = chunks.get(i);
                double x = chunk.x * 16.0, z = chunk.z * 16.0;
                candidates.put(new JSONObject()
                    .put("chunk", new JSONArray(new int[]{chunk.x, chunk.z}))
                    .put("eye_target", new JSONObject().put("x", x).put("z", z))
                    .put("dig", new JSONObject().put("x", chunk.fourFourX()).put("z", chunk.fourFourZ()))
                    .put("probability", chunk.weight / total)
                    .put("distance_blocks", Math.hypot(x - latest.xInOverworld(), z - latest.zInOverworld())));
            }
            System.out.println(new JSONObject().put("ok", true).put("minecraft", "Java 1.19+")
                .put("throws", throwsList.size()).put("candidate_count", chunks.size())
                .put("advanced", input.optBoolean("advanced", true)).put("candidates", candidates));
        } catch (Exception error) {
            System.err.println(error.getClass().getSimpleName() + ": " + error.getMessage());
            System.exit(1);
        }
    }
}
