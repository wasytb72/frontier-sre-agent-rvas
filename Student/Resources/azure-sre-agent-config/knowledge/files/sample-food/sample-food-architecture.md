# Sample Food Ordering App Architecture

The Sample Food Ordering App lab is an optional application workload integrated into the existing Azure VNet Flow Logs and Traffic Analytics Terraform project.

## Components

| Component | Azure service | Terraform output/source |
| --- | --- | --- |
| Frontend | Azure Container Apps | `sample_food_frontend_container_app_name`, `sample_food_frontend_url` |
| API | Azure Container Apps | `sample_food_api_container_app_name`, `sample_food_api_url` |
| Container registry | GitHub Packages, public. Images are pulled anonymously, so the container apps hold no registry credential and no pull identity | `ghcr.io/<owner>/<repository>/grubify-api` and `.../grubify-frontend` |
| Runtime environment | Azure Container Apps Environment | `sample_food_resource_names.container_apps_environment` |
| App telemetry | Application Insights | `sample_food_application_insights_resource_id` |
| Log store | Log Analytics workspace | `log_analytics_workspace_customer_id` |
| Network boundary | Dedicated VNet | `sample_food_network.vnet_id`, default `10.40.0.0/16` |

## Traffic And Telemetry

- Browser users call the frontend Container App over HTTPS.
- The frontend calls the API base URL configured by `REACT_APP_API_BASE_URL`.
- The API (ASP.NET Core, container target port `8080`) exposes these routes: `GET /WeatherForecast`, `GET /api/FoodItems`, `GET /api/Restaurants`, and `GET`/`POST /api/cart/{user}/items` (e.g. `/api/cart/demo-user/items`, body `{"foodItemId":1,"quantity":1}`). There is **no `/health` and no `/api/menu`** route, and no HTTP health probe (only a platform TCP probe on port 8080); use `GET /WeatherForecast` (expect 200) as the liveness check and `GET /api/FoodItems` (expect 200) as the domain-health check.
- Azure Container Apps emits platform, console, and HTTP logs to Log Analytics when diagnostic settings are enabled.
- Application Insights is available for application telemetry when the app emits it.

## Reversible Lab Outage

From `Student/`, run `make break-food` to inject an ingress routing fault, and
`make restore-food` to recover. These commands require Azure CLI write access
to the lab API Container App's ingress and resource tags. Use only in an isolated
lab; the fault intentionally makes the public API unavailable.

The script saves the current ingress target port in the Container App tag
`rvas-food-original-target-port`, then changes it to port `1`, where the lab API
does not listen. It verifies HTTP 5xx on both `/WeatherForecast` and
`/api/FoodItems` for three consecutive probes. DNS errors, connection failures,
and timeouts are not counted as successful fault injection. Verification is
bounded to 24 attempts (up to two 10-second requests per attempt, with five
seconds between attempts); it exits nonzero if the expected state is not observed.

Recovery restores the saved port, verifies three consecutive HTTP 2xx responses
on both routes, then removes the recovery tag. Do not remove the tag before
recovery. If an update or verification fails, the saved state is retained so
`make restore-food` can be retried. A second injection is refused until recovery.
Avoid concurrent scenario runs or ingress edits during the exercise.

The script can also be run directly:

```bash
bash Resources/scenarios/scripts/break-sample-food-app.sh break --yes
bash Resources/scenarios/scripts/break-sample-food-app.sh restore --yes
```

These replace the former URL/request-count/sleep arguments.

This scenario intentionally creates temporary Terraform configuration drift.
Restore it before applying Terraform again. It does not modify the API image,
application source, or memory limits. Unlike the former cart-load loop, it is
an ingress outage, **not an OOM or application memory-leak incident**. Investigate
the ingress target port and Azure Activity Log; application request telemetry may
be absent because the requests never reach the application. Restarting a revision
does not fix this fault.

## Important Boundary

Azure Container Apps workload traffic is not supported by Azure Virtual Network Flow Logs. Use Container Apps logs and Application Insights for application incident analysis. Use `NTANetAnalytics` for the existing VM/network lab and any explicitly deployed supported probe workload.

Official source: https://learn.microsoft.com/en-us/azure/network-watcher/vnet-flow-logs-overview#incompatible-services
